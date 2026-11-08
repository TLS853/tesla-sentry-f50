/* Optional browser smoke test against a local source server; API data is simulated.
 * TSF_PLAYWRIGHT_PATH=playwright-core TSF_BROWSER_BIN=/path/to/chrome node tests/ui.cjs URL OUTPUT_DIR
 * Start the safe source server with: python3 tools/preview_ui.py
 */
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.TSF_PLAYWRIGHT_PATH || 'playwright');
const base = process.argv[2] || 'http://127.0.0.1:8080';
const output = path.resolve(process.argv[3] || '/tmp/tesla-sentry-ui');
if (!['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname)) throw new Error('Use a local preview server');

function event(name, reason, camera, city, street, cameras = 4) {
  return { name, size: 88220164, meta: { reason, camera, city, street, timestamp: name.replace('_', 'T').replace(/-(\d\d)-(\d\d)$/, ':$1:$2') },
    videos: ['front', 'back', 'left_repeater', 'right_repeater', 'left_pillar', 'right_pillar'].slice(0, cameras)
      .map(cam => ({ f: name + '-' + cam + '.mp4', cam, url: '/media/SentryClips/' + name + '/' + name + '-' + cam + '.mp4' })) };
}
const events = {
  SentryClips: [
    event('2026-11-08_18-42-00', 'sentry_aware_object_detection', 0, '上海', '淮海中路'),
    event('2026-11-08_16-18-00', 'sentry_aware_door', 3, '上海', '静安嘉里中心 · B2 停车场'),
    event('2026-11-08_12-06-00', 'sentry_aware_accel', 7, '上海', '滨江大道', 6),
    event('2026-11-07_20-31-00', 'sentry_aware_object_detection', 0, '上海', '武康路'),
    event('2026-11-07_15-24-00', 'sentry_aware_object_detection', 5, '上海', '世纪公园'),
  ],
  SavedClips: [event('2026-11-08_11-15-00', 'user_interaction_dashcam_saved', 0, '上海', '中环路')],
  RecentClips: [],
};
function contrast(a, b) {
  const luminance = hex => {
    if (hex.length === 4) hex = '#' + hex.slice(1).split('').map(c => c + c).join('');
    const c = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map(v => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    return c[0] * 0.2126 + c[1] * 0.7152 + c[2] * 0.0722;
  };
  const [lo, hi] = [luminance(a), luminance(b)].sort((x, y) => x - y);
  return (hi + 0.05) / (lo + 0.05);
}
function thumbnail(url) {
  const garage = url.includes('16-18'), night = url.includes('20-31');
  const sky = night ? '#293540' : garage ? '#647078' : '#afc1c5';
  const building = night ? '#18242c' : garage ? '#46545e' : '#7d939b';
  return `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360"><rect width="640" height="360" fill="${sky}"/><path fill="${building}" d="M0 80h80v95h40V55h70v120h25V95h85v98h50V35h85v148h35V75h100v125h70v-90h70v200H0Z"/><path fill="#425257" d="M0 220h640v140H0Z"/><path fill="#7e8d8d" d="M0 220h240l-65 140H0Zm400 0h240v140H465Z"/><path stroke="#dce1d8" stroke-width="4" stroke-dasharray="22 25" d="m291 225-12 135m70-135 12 135"/><path fill="#303b41" d="m105 235 12-26h46l13 26v31h-71Z"/><path fill="#b5c5ca" d="m121 214-8 20h55l-9-20Z"/><path fill="#c1c9c9" d="m470 245 10-24h40l13 24v23h-63Z"/><path fill="#607279" d="m484 226-6 17h46l-7-17Z"/><rect y="330" width="640" height="30" fill="#172026" opacity=".6"/><text x="18" y="350" font-family="monospace" font-size="11" fill="#e4eaec">DEMO FOOTAGE / UI PREVIEW</text></svg>`;
}

async function main() {
  await fs.mkdir(output, { recursive: true });
  const video = process.env.TSF_UI_VIDEO ? await fs.readFile(process.env.TSF_UI_VIDEO) : Buffer.from('preview');
  const browser = await chromium.launch({ headless: true, ...(process.env.TSF_BROWSER_BIN ? { executablePath: process.env.TSF_BROWSER_BIN } : {}) });
  const results = [];
  try {
    for (const view of [
      { name: 'desktop-light', width: 1440, height: 1000, theme: 'light' },
      { name: 'desktop-dark', width: 1440, height: 1000, theme: 'dark' },
      { name: 'mobile-light', width: 390, height: 844, theme: 'light' },
      { name: 'mobile-dark', width: 360, height: 780, theme: 'dark' },
      { name: 'small-mobile', width: 320, height: 740, theme: 'light' },
      { name: 'tablet-light', width: 834, height: 1112, theme: 'light' },
      { name: 'desktop-english', width: 1340, height: 1000, theme: 'dark', language: 'en' },
    ]) {
      const context = await browser.newContext({ viewport: { width: view.width, height: view.height }, locale: view.language === 'en' ? 'en-US' : 'zh-CN', timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce' });
      await context.addInitScript(settings => { localStorage.setItem('tsf_theme', settings.theme); localStorage.setItem('tsf_lang', settings.language || 'zh'); }, view);
      const page = await context.newPage(), errors = [], requests = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('request', request => requests.push({ url: request.url(), method: request.method() }));
      await page.route('**/api/**', async route => {
        const pathname = new URL(route.request().url()).pathname;
        let data = { ok: true };
        if (pathname === '/api/events') data = events;
        else if (pathname === '/api/live') data = { running: true, pending: 0, ago: 3, disk: { src: 'loop', total: 256 * 1073741824, used: 82.4 * 1073741824 } };
        else if (pathname === '/api/skin') return route.fulfill({ contentType: 'text/css', body: '' });
        else if (pathname === '/api/skins') data = { skins: [], active: '' };
        else if (pathname === '/api/audio') data = { queue: [], done: [] };
        else if (pathname === '/api/usb/browse') data = { ok: false, attached: false, dirs: [], files: [] };
        else if (pathname === '/api/notify') data = { provider: 'qq', mode: 'dm', qq_target_type: 'user', media: 'thumb', quality: '480p', delay: 20, vdur: 30, enabled: false };
        else if (pathname === '/api/auth/info') data = { on: true, user: 'admin' };
        else if (pathname === '/api/telemetry') data = { ok: false };
        else if (pathname === '/api/ota/status') data = { current: '2.10.0', busy: false, rollback_ready: true, mode: 'manual_zip' };
        else if (pathname === '/api/ota/apply') data = { ok: false, msg: 'UI preview does not install updates' };
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
      });
      await page.route('**/thumb/**', route => route.fulfill({ contentType: 'image/svg+xml', body: thumbnail(route.request().url()) }));
      await page.route('**/media/**', route => {
        const range = /bytes=(\d+)-(\d*)/.exec(route.request().headers().range || '');
        const start = range ? Number(range[1]) : 0, end = range && range[2] ? Math.min(Number(range[2]), video.length - 1) : video.length - 1;
        return route.fulfill({ status: range ? 206 : 200, contentType: 'video/mp4', headers: { 'Accept-Ranges': 'bytes', ...(range ? { 'Content-Range': `bytes ${start}-${end}/${video.length}` } : {}) }, body: video.subarray(start, end + 1) });
      });
      await page.goto(base, { waitUntil: 'networkidle' });
      await page.locator('.card').first().waitFor();
      assert.equal(await page.locator('.card').count(), 5);
      assert.equal(requests.filter(r => r.url.includes('/api/ota/')).length, 0, 'Startup must not check for updates');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, view.name + ': library overflow');
      const colors = await page.evaluate(() => {
        const css = getComputedStyle(document.documentElement);
        return Object.fromEntries(['--bg', '--panel', '--text', '--muted', '--muted2', '--accent', '--accent-fill', '--positive'].map(key => [key, css.getPropertyValue(key).trim()]));
      });
      for (const foreground of ['--text', '--muted', '--muted2', '--accent', '--positive']) {
        for (const background of ['--bg', '--panel']) assert.ok(contrast(colors[foreground], colors[background]) >= 4.5, view.name + ': text contrast ' + foreground);
      }
      assert.ok(contrast('#ffffff', colors['--accent-fill']) >= 4.5, view.name + ': primary button contrast');
      await page.screenshot({ path: path.join(output, view.name + '.png'), fullPage: true });

      await page.locator('#filters .chip').filter({ hasText: '1' }).first().click();
      assert.equal(await page.locator('.card').count(), 1);
      await page.locator('#clear-filters').click();
      assert.equal(await page.locator('.card').count(), 5);
      await page.locator('#tabs .tab').nth(2).click();
      assert.equal(await page.locator('.card').count(), 0);
      assert.equal(await page.locator('#empty').isVisible(), true);
      assert.equal(await page.locator('#empty .empty-symbol svg').count(), 1);
      await page.locator('#tabs .tab').first().click();
      await page.locator('.card').first().focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('#player').evaluate(el => el.classList.contains('open')), true);
      await page.locator('.pdl').click();
      assert.equal(await page.locator('#expp').evaluate(el => el.classList.contains('open')), true);
      await page.locator('#expp .x').click();
      if (view.width > 1000) await page.screenshot({ path: path.join(output, view.name + '-player.png') });
      await page.locator('.ptop .pback').click();

      await page.locator('#gear').click();
      assert.equal(await page.locator('#panel').getAttribute('aria-hidden'), 'false');
      assert.equal(await page.locator('.library').evaluate(el => el.inert), true);
      for (const tab of ['files', 'audio', 'wraps', 'notify', 'auth', 'lang', 'skin']) {
        await page.locator('#st-' + tab).click();
        assert.equal(await page.locator('#tab-' + tab).isVisible(), true, view.name + ': settings tab ' + tab);
        assert.equal(await page.locator('#settings-content').evaluate(el => el.scrollWidth <= el.clientWidth + 1), true, view.name + ': content overflow in ' + tab);
        if (tab === 'notify') await page.screenshot({ path: path.join(output, view.name + '-notify.png') });
      }
      await page.locator('#st-update').click();
      await page.locator('#upd-cur').filter({ hasText: '2.10.0' }).waitFor();
      await page.locator('#upd-url').fill('https://updates.example/release.zip?X-Amz-Signature=preview');
      await page.locator('#upd-sha').fill('bad-hash');
      await page.locator('#upd-go').click();
      assert.match(await page.locator('#upd-msg').innerText(), /SHA-256/);
      assert.equal(requests.filter(r => r.method === 'POST' && r.url.includes('/api/ota/apply')).length, 0);
      await page.locator('#upd-sha').fill('');
      assert.equal(await page.locator('#panel').evaluate(el => el.scrollWidth <= el.clientWidth + 1), true, view.name + ': settings overflow');
      await page.screenshot({ path: path.join(output, view.name + '-settings.png') });
      await page.locator('#panel .x').focus();
      await page.keyboard.press('Shift+Tab');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'upd-rb');
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#panel').getAttribute('aria-hidden'), 'true');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'gear');
      await page.locator('#gear').click();
      await page.keyboard.press('ArrowRight');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'st-audio');
      await page.keyboard.press('Escape');
      assert.deepEqual(errors, [], view.name + ': JavaScript errors');
      results.push({ view: view.name, cards: 5, overflow: false, focus: 'passed', contrast: '4.5:1 or higher' });

      await page.goto(base + '/login', { waitUntil: 'networkidle' });
      await page.locator('#login-user').waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, view.name + ': login overflow');
      assert.equal(await page.locator('#login-pass').getAttribute('autocomplete'), 'current-password');
      assert.equal(await page.locator('body').innerText().then(t => /__L_/.test(t)), false);
      if (view.name === 'desktop-light' || view.name === 'mobile-dark') await page.screenshot({ path: path.join(output, view.name + '-login.png') });
      await context.close();
    }
    console.log(JSON.stringify({ output, results }, null, 2));
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
