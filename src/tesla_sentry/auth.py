"""Auth service."""
import hashlib
import hmac
import os
import threading
import time
from . import config

AUTH_FILE = config.get("TSF_AUTH_FILE", os.path.join(config.settings.install_dir, "auth.conf"))


def _auth_conf():
    d = {}
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip().lower()] = v.strip()
    except OSError:
        pass
    return d


_AC = _auth_conf()
AUTH_USER = config.get("TSF_AUTH_USER") or _AC.get("user") or "admin"
AUTH_PASS = config.get("TSF_AUTH_PASS") or _AC.get("pass") or ""
AUTH_SHA = (config.get("TSF_AUTH_SHA256") or _AC.get("sha256") or "").strip().lower()
AUTH_SECRET = (config.get("TSF_AUTH_SECRET") or _AC.get("secret") or "") or ("tsf-v1|" + (AUTH_SHA or AUTH_PASS))
AUTH_TTL = int(config.get("TSF_AUTH_TTL") or _AC.get("ttl") or 30 * 86400)
AUTH_ON = bool(AUTH_SHA or AUTH_PASS)
FAILS = {}                       # ip -> [失败次数, 首次失败时间]
FAIL_LOCK = threading.Lock()


def pw_ok(pw):
    if AUTH_SHA:
        return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), AUTH_SHA)
    return hmac.compare_digest((pw or "").encode(), AUTH_PASS.encode())


def auth_set_pass(old, new):
    """改网页登录密码; 返回 (成功?, 提示)。成功后重写 auth.conf 与内存凭据,
    并换一个随机 secret → 其它设备上的旧 cookie 立刻失效, 只有本机(前端会用新密码重新登录)继续。"""
    global AUTH_PASS, AUTH_SHA, AUTH_SECRET, AUTH_ON
    if config.get("TSF_AUTH_PASS") or config.get("TSF_AUTH_SHA256"):
        return False, "密码由环境变量 TSF_AUTH_PASS 控制, 改文件不生效"
    if AUTH_ON and not pw_ok(old or ""):
        return False, "原密码不对"
    n = (new or "").strip()
    if len(n) < 6:
        return False, "新密码至少 6 位"
    if AUTH_ON and hmac.compare_digest(n.encode(), (AUTH_PASS or "").encode()):
        return False, "新密码不能和原密码一样"
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    newsec = hashlib.sha256(os.urandom(32)).hexdigest()
    out, has_pass, has_sec = [], False, False
    for ln in lines:
        s = ln.strip()
        k = s.split("=", 1)[0].strip().lower() if ("=" in s and not s.startswith("#")) else ""
        if k == "pass":
            out.append("pass=" + n)
            has_pass = True
        elif k == "sha256":
            continue                       # 老写法统一换成 pass=
        elif k == "secret":
            out.append("secret=" + newsec)
            has_sec = True
        else:
            out.append(ln)
    if not has_pass:
        out.append("pass=" + n)
    if not has_sec:
        out.append("secret=" + newsec)
    try:
        os.makedirs(os.path.dirname(AUTH_FILE) or ".", exist_ok=True)
        tmp = AUTH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, AUTH_FILE)
    except OSError as ex:
        return False, "写入失败: %s" % str(ex)[:80]
    AUTH_PASS, AUTH_SHA, AUTH_SECRET, AUTH_ON = n, "", newsec, True
    return True, "密码已更新, 其它设备需要重新登录"


def sess_token(exp):
    raw = "%s|%d" % (AUTH_USER, exp)
    sig = hmac.new(AUTH_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return "%d.%s" % (exp, sig)


def sess_ok(tok):
    try:
        exp = int(tok.split(".", 1)[0])
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(tok, sess_token(exp))


def fail_check(ip, inc):
    """同一 IP 10 分钟内连错 8 次就锁 5 分钟, 防爆破"""
    now = time.time()
    with FAIL_LOCK:
        cnt, t0 = FAILS.get(ip, [0, now])
        if now - t0 > 600:
            cnt, t0 = 0, now
        if inc:
            cnt += 1
        FAILS[ip] = [cnt, t0]
        return cnt >= 8
