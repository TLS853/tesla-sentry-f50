export const CAM_NAME = { front:'前视', back:'后视', left_repeater:'左B柱', right_repeater:'右B柱',
                   left_pillar:'左前翼', right_pillar:'右前翼' };
/* 排列顺序按「位置成对」：
   4 路(3/Y)      第1排 前视 后视 / 第2排 左B柱 右B柱
   6 路(S/X,HW4)  第1排 左前翼 前视 右前翼 / 第2排 左B柱 后视 右B柱
   这样左右对称, 前后同一排, 一眼能看出方位 */
export const CAM_ORDER_4 = ['front','back','left_repeater','right_repeater','left_pillar','right_pillar'];
export const CAM_ORDER_6 = ['left_pillar','front','right_pillar','left_repeater','back','right_repeater'];

export function camSort(vs) {
  const order = vs.length > 4 ? CAM_ORDER_6 : CAM_ORDER_4;
  return vs.slice().sort((a, b) => {
    const ia = order.indexOf(a.cam), ib = order.indexOf(b.cam);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
}
/* 事件目录里通常有多组片段 (每分钟一组, 每组 4~6 路), 按组切开 */
export function clipKeyOf(v) {
  const f = v.f || v.url.split('/').pop();
  return f.replace(/\.mp4$/i, '').replace(/-[^-]+$/, '');
}
export function groupVideos(vids) {
  const m = new Map();
  for (const v of camSort(vids || [])) {
    const k = clipKeyOf(v);
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(v);
  }
  return [...m.keys()].sort().map(k => ({ key: k, vids: m.get(k) }));
}
