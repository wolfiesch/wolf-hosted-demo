// Page diff that survives layout drift. Rows and columns of ink are aligned first (a banded
// edit-distance alignment), so a row that grew by a pixel moves everything below it without
// marking it. Aligned pixels are then compared with a one-pixel window in both directions, on
// lightly blurred brightness so anti-aliasing noise from sub-pixel shifts does not count.
// What is left is grouped into boxes: "color" where both pages have the same shape in another
// color, "moved" where the box's content sits nearby in the original, "content" otherwise.
const INK = 200, LUM = 40, HUE = 90, SHARE = 0.85, BAND = 96, CELL = 8, REACH = 24, RULE = 16, JOIN = 24, MOVED_INK = 40;

function luma(img) {
  const { data, width, height } = img, out = new Uint8Array(width * height);
  for (let i = 0, p = 0; p < out.length; p++, i += 4) out[p] = (data[i] * 77 + data[i + 1] * 150 + data[i + 2] * 29) >> 8;
  return out;
}

function darkness(img) {
  const { data } = img, out = new Uint8Array(data.length >> 2);
  for (let i = 0, p = 0; p < out.length; p++, i += 4) out[p] = 255 - Math.min(data[i], data[i + 1], data[i + 2]);
  return out;
}

// Long horizontal or vertical runs of ink: table rules, underlines, borders.
function rules(lum, w, h) {
  const out = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0, run = 0; x <= w; x++) {
    if (x < w && lum[y * w + x] < INK) { run++; continue; }
    if (run >= RULE) for (let k = x - run; k < x; k++) out[y * w + k] = 1;
    run = 0;
  }
  for (let x = 0; x < w; x++) for (let y = 0, run = 0; y <= h; y++) {
    if (y < h && lum[y * w + x] < INK) { run++; continue; }
    if (run >= RULE) for (let k = y - run; k < y; k++) out[k * w + x] = 1;
    run = 0;
  }
  return out;
}

function nearRule(r, w, h, x, y, d) {
  for (let yy = Math.max(0, y - d); yy <= Math.min(h - 1, y + d); yy++)
    for (let xx = Math.max(0, x - d); xx <= Math.min(w - 1, x + d); xx++) if (r[yy * w + xx]) return true;
  return false;
}

// 1-2-1 blur: softens anti-aliasing while keeping a one-pixel stroke at half strength.
function blur(lum, w, h) {
  const out = new Uint8Array(w * h), K = [1, 2, 1];
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    let s = 0, n = 0;
    for (let dy = -1; dy <= 1; dy++) { const yy = y + dy; if (yy < 0 || yy >= h) continue; for (let dx = -1; dx <= 1; dx++) { const xx = x + dx; if (xx < 0 || xx >= w) continue; const k = K[dy + 1] * K[dx + 1]; s += lum[yy * w + xx] * k; n += k; } }
    out[y * w + x] = s / n;
  }
  return out;
}

// One bitset of ink per row (or column), packed into 32-bit words.
function signatures(lum, w, h, byColumn) {
  const n = byColumn ? w : h, len = byColumn ? h : w, words = (len + 31) >> 5;
  const sig = new Uint32Array(n * words), count = new Uint32Array(n);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    if (lum[y * w + x] >= INK) continue;
    const line = byColumn ? x : y, pos = byColumn ? y : x;
    sig[line * words + (pos >> 5)] |= 1 << (pos & 31);
    count[line]++;
  }
  return { sig, count, words, n };
}

function popcount(v) { v -= (v >>> 1) & 0x55555555; v = (v & 0x33333333) + ((v >>> 2) & 0x33333333); return (((v + (v >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24; }

// Returns map[j] = matching line in A for line j of B, or -1 for a line B inserted.
function align(A, B) {
  const n = A.n, m = B.n, words = Math.min(A.words, B.words);
  const lo = Math.min(0, m - n) - BAND, hi = Math.max(0, m - n) + BAND, span = hi - lo + 1;
  const INF = 1e15, cost = new Float64Array((n + 1) * span).fill(INF), step = new Uint8Array((n + 1) * span);
  const at = (i, j) => { const k = j - i - lo; return k < 0 || k >= span ? -1 : i * span + k; };
  const mismatch = (i, j) => lineMismatch(A, i, B, j);
  cost[at(0, 0)] = 0;
  for (let i = 0; i <= n; i++) for (let j = Math.max(0, i + lo); j <= Math.min(m, i + hi); j++) {
    const here = at(i, j); if (here < 0 || (i === 0 && j === 0)) continue;
    let best = INF, how = 0, k;
    if (i > 0 && j > 0 && (k = at(i - 1, j - 1)) >= 0 && cost[k] < INF) { const c = cost[k] + mismatch(i - 1, j - 1); if (c < best) { best = c; how = 1; } }
    if (i > 0 && (k = at(i - 1, j)) >= 0 && cost[k] < INF) { const c = cost[k] + A.count[i - 1] + 1; if (c < best) { best = c; how = 2; } }
    if (j > 0 && (k = at(i, j - 1)) >= 0 && cost[k] < INF) { const c = cost[k] + B.count[j - 1] + 1; if (c < best) { best = c; how = 3; } }
    cost[here] = best; step[here] = how;
  }
  const map = new Int32Array(m).fill(-1), back = new Int32Array(n).fill(-1);
  let i = n, j = m;
  if (at(n, m) < 0 || cost[at(n, m)] >= INF) return null;
  while (i > 0 || j > 0) {
    const how = step[at(i, j)];
    if (how === 1) { i--; j--; map[j] = i; back[i] = j; } else if (how === 2) i--; else j--;
  }
  return { map, back };
}

// How many ink pixels differ between line i of A and line j of B.
function lineMismatch(A, i, B, j) {
  const words = Math.min(A.words, B.words);
  let d = 0;
  for (let w = 0; w < words; w++) d += popcount(A.sig[i * A.words + w] ^ B.sig[j * B.words + w]);
  for (let w = words; w < A.words; w++) d += popcount(A.sig[i * A.words + w]);
  for (let w = words; w < B.words; w++) d += popcount(B.sig[j * B.words + w]);
  return d;
}

// A line one page inserted has no partner. Give it the most similar line of the other page that
// keeps the order: between its neighbours' partners. A rule drawn thicker picks the rule; blank
// space that grew picks blank space.
function fill(map, S, D) {
  const out = Int32Array.from(map), n = map.length;
  const prev = new Int32Array(n).fill(-1), next = new Int32Array(n).fill(-1);
  for (let j = 0, p = -1; j < n; j++) { if (map[j] >= 0) p = j; prev[j] = p; }
  for (let j = n - 1, q = -1; j >= 0; j--) { if (map[j] >= 0) q = j; next[j] = q; }
  for (let j = 0; j < n; j++) {
    if (map[j] >= 0) continue;
    const lo = prev[j] >= 0 ? map[prev[j]] : 0, hi = Math.min(next[j] >= 0 ? map[next[j]] : D.n - 1, lo + 64);
    let best = Math.min(lo, D.n - 1), bestCost = Infinity;
    for (let c = lo; c <= hi && c < D.n; c++) { const cost = lineMismatch(D, c, S, j); if (cost < bestCost) { bestCost = cost; best = c; } }
    out[j] = best;
  }
  return out;
}

// The other page, resampled into this page's frame through the line maps.
function warp(px, dw, mapX, mapY) {
  const w = mapX.length, h = mapY.length, out = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) {
    const row = mapY[y] * dw;
    for (let x = 0; x < w; x++) { const s = (row + mapX[x]) * 4, d = (y * w + x) * 4; out[d] = px[s]; out[d + 1] = px[s + 1]; out[d + 2] = px[s + 2]; out[d + 3] = 255; }
  }
  return out;
}

function compare(a, b) {
  const wa = a.width, ha = a.height, wb = b.width, hb = b.height;
  const resized = wa !== wb || ha !== hb;
  if (!resized) {
    let same = true;
    for (let i = 0; i < a.data.length; i += 4) if (a.data[i] !== b.data[i] || a.data[i + 1] !== b.data[i + 1] || a.data[i + 2] !== b.data[i + 2]) { same = false; break; }
    if (same) return { changed: false, regions: [], shift: 0, resized };
  }
  const la = luma(a), lb = luma(b);
  const rowSigA = signatures(la, wa, ha, false), rowSigB = signatures(lb, wb, hb, false);
  const colSigA = signatures(la, wa, ha, true), colSigB = signatures(lb, wb, hb, true);
  const rows = align(rowSigA, rowSigB), cols = align(colSigA, colSigB);
  const identity = (n, m) => { const map = new Int32Array(m); for (let j = 0; j < m; j++) map[j] = j < n ? j : -1; const back = new Int32Array(n); for (let i = 0; i < n; i++) back[i] = i < m ? i : -1; return { map, back }; };
  const R = rows || identity(ha, hb), C = cols || identity(wa, wb);
  const rowB2A = fill(R.map, rowSigB, rowSigA), colB2A = fill(C.map, colSigB, colSigA);
  const rowA2B = fill(R.back, rowSigA, rowSigB), colA2B = fill(C.back, colSigA, colSigB);

  let shift = 0;
  for (let j = 0; j < hb; j++) if (R.map[j] >= 0) shift = Math.max(shift, Math.abs(j - R.map[j]));
  for (let j = 0; j < wb; j++) if (C.map[j] >= 0) shift = Math.max(shift, Math.abs(j - C.map[j]));

  // Each page is compared against the other page warped into its own frame, so the blur that
  // absorbs anti-aliasing never mixes rows the alignment pulled apart. Shape is measured as
  // darkness (how far the darkest channel is from white), so recolored ink on a light
  // background keeps its shape; color is measured as brightness plus hue.
  const view = (px, w, h) => {
    const img = { data: px, width: w, height: h }, l = luma(img), k = darkness(img);
    return { px, l: blur(l, w, h), k: blur(k, w, h), rawL: l, rawK: k, rules: rules(l, w, h), w, h };
  };
  // 0 = same, 1 = same shape in another color, 2 = different shape. Color is only judged where
  // both pixels carry the same amount of ink; otherwise the difference is anti-aliasing.
  const bad = (S, D, p, q) => {
    if (Math.abs(S.k[p] - D.k[q]) > LUM) return 2;
    if (Math.abs(S.rawK[p] - D.rawK[q]) > LUM) return 0;
    if (Math.abs(S.l[p] - D.l[q]) > LUM) return 1;
    const s = S.px, d = D.px;
    return Math.abs((s[p * 4] - s[p * 4 + 1]) - (d[q * 4] - d[q * 4 + 1])) + Math.abs((s[p * 4 + 1] - s[p * 4 + 2]) - (d[q * 4 + 1] - d[q * 4 + 2])) > HUE ? 1 : 0;
  };
  // Each 8x8 block takes the one offset (within a pixel) where it best matches, then its pixels
  // are compared there. One offset per block, not per pixel, so a changed glyph cannot hide
  // behind a neighbouring stroke. A table rule drawn a pixel thicker or a pixel off is
  // forgiven: shape changes on or beside a rule when the other page has a rule there too.
  const diff = (S, D) => {
    const w = S.w, h = S.h, out = new Uint8Array(w * h);
    for (let by = 0; by < h; by += CELL) for (let bx = 0; bx < w; bx += CELL) {
      const ey = Math.min(h, by + CELL), ex = Math.min(w, bx + CELL);
      let best = Infinity, bk = 0;
      for (let k = 0; k < 9 && best; k++) {
        let n = 0;
        for (let y = by; y < ey && n < best; y++) for (let x = bx; x < ex; x++) {
          const xx = x + WIN_X[k], yy = y + WIN_Y[k];
          if (xx < 0 || yy < 0 || xx >= w || yy >= h || bad(S, D, y * w + x, yy * w + xx)) n++;
        }
        if (n < best) { best = n; bk = k; }
      }
      if (!best) continue;
      for (let y = by; y < ey; y++) for (let x = bx; x < ex; x++) {
        const xx = x + WIN_X[bk], yy = y + WIN_Y[bk];
        let v = xx < 0 || yy < 0 || xx >= w || yy >= h ? 2 : bad(S, D, y * w + x, yy * w + xx);
        if (v === 2 && nearRule(S.rules, w, h, x, y, 2) && nearRule(D.rules, w, h, x, y, 3)) v = 0;
        if (v) out[y * w + x] = v;
      }
    }
    return out;
  };
  const changedB = diff(view(b.data, wb, hb), view(warp(a.data, wa, colB2A, rowB2A), wb, hb));
  const changedA = diff(view(a.data, wa, ha), view(warp(b.data, wb, colA2B, rowA2B), wa, ha));
  const mark = new Uint8Array(wb * hb);
  for (let p = 0; p < wb * hb; p++) if (changedB[p]) mark[p] = 1;
  for (let y = 0; y < ha; y++) for (let x = 0; x < wa; x++) if (changedA[y * wa + x]) mark[rowA2B[y] * wb + colA2B[x]] = 1;

  const found = regions(mark, wb, hb);
  for (const r of found.regions) {
    // The changed pixels of each page inside this box, in their own page's coordinates.
    const ax0 = colB2A[r.x], ay0 = rowB2A[r.y], ax1 = colB2A[r.x + r.w - 1], ay1 = rowB2A[r.y + r.h - 1];
    const inB = [], inA = [];
    let hue = 0, all = 0;
    for (let y = r.y; y < r.y + r.h; y++) for (let x = r.x; x < r.x + r.w; x++) { const v = changedB[y * wb + x]; if (v) { inB.push(x, y); all++; if (v === 1) hue++; } }
    for (let y = Math.max(0, ay0); y <= ay1; y++) for (let x = Math.max(0, ax0); x <= ax1; x++) { const v = changedA[y * wa + x]; if (v) { inA.push(x, y); all++; if (v === 1) hue++; } }
    r.kind = hue >= all * 0.5 ? "color"
      : movedBy(inB, lb, wb, la, wa, ha, colB2A, rowB2A, inA, lb, wb, hb, la, wa, colA2B, rowA2B) ? "moved" : "content";
  }
  return { ...found, regions: joinLines(found.regions), shift, resized };
}

const WIN_X = [0, -1, 1, 0, 0, -1, 1, -1, 1], WIN_Y = [0, 0, 0, -1, 1, -1, -1, 1, 1];

// Boxes of the same kind side by side on one line (words of a recolored sentence) become one.
function joinLines(list) {
  const out = list.slice().sort((p, q) => p.y - q.y || p.x - q.x);
  for (let merged = true; merged;) {
    merged = false;
    for (let i = 0; i < out.length && !merged; i++) for (let j = i + 1; j < out.length && !merged; j++) {
      const p = out[i], q = out[j];
      if (p.kind !== q.kind) continue;
      const overlap = Math.min(p.y + p.h, q.y + q.h) - Math.max(p.y, q.y);
      const gap = Math.max(p.x, q.x) - Math.min(p.x + p.w, q.x + q.w);
      if (overlap < Math.min(p.h, q.h) * 0.6 || gap > JOIN) continue;
      const x = Math.min(p.x, q.x), y = Math.min(p.y, q.y);
      out[i] = { ...p, x, y, w: Math.max(p.x + p.w, q.x + q.w) - x, h: Math.max(p.y + p.h, q.y + q.h) - y, pixels: p.pixels + q.pixels };
      out.splice(j, 1);
      merged = true;
    }
  }
  return out;
}

// Is there one offset that carries the changed ink of each page onto ink of the other?
function movedBy(ptsB, lb, wb, la, wa, ha, colB2A, rowB2A, ptsA, lb2, wb2, hb, la2, wa2, colA2B, rowA2B) {
  const inkOnly = (pts, lum, w) => { const out = []; for (let i = 0; i < pts.length; i += 2) if (lum[pts[i + 1] * w + pts[i]] < INK) out.push(pts[i], pts[i + 1]); return out; };
  const sample = (pts) => { const n = pts.length / 2, step = Math.max(1, Math.floor(n / 300)), out = []; for (let i = 0; i < n; i += step) out.push(pts[i * 2], pts[i * 2 + 1]); return out; };
  const fits = (pts, lum, w, dl, dw, dh, mapX, mapY, dx, dy) => {
    let hit = 0;
    const n = pts.length / 2, need = n * SHARE;
    for (let i = 0; i < pts.length; i += 2) {
      const xx = mapX[pts[i]] + dx, yy = mapY[pts[i + 1]] + dy;
      if (xx >= 0 && yy >= 0 && xx < dw && yy < dh && dl[yy * dw + xx] < INK) hit++;
      else if (i / 2 - hit > n - need) return false;
    }
    return hit >= need;
  };
  const inkB = inkOnly(ptsB, lb, wb), inkA = inkOnly(ptsA, la2, wa2);
  // A glyph or two is too little ink to tell a move from a swap.
  if ((inkB.length + inkA.length) / 2 < MOVED_INK) return false;
  const sB = sample(inkB), sA = sample(inkA);
  for (let dy = -REACH; dy <= REACH; dy++) for (let dx = -REACH; dx <= REACH; dx++) {
    if (!dx && !dy) continue;
    if ((!sB.length || fits(sB, lb, wb, la, wa, ha, colB2A, rowB2A, dx, dy)) && (!sA.length || fits(sA, la2, wa2, lb2, wb2, hb, colA2B, rowA2B, -dx, -dy))
      && (!inkB.length || fits(inkB, lb, wb, la, wa, ha, colB2A, rowB2A, dx, dy)) && (!inkA.length || fits(inkA, la2, wa2, lb2, wb2, hb, colA2B, rowA2B, -dx, -dy))) return true;
  }
  return false;
}

// Group changed pixels into boxes on a coarse grid; isolated specks are noise.
function regions(mark, w, h) {
  const gw = Math.ceil(w / CELL), gh = Math.ceil(h / CELL), cnt = new Uint16Array(gw * gh);
  let pixels = 0;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) if (mark[y * w + x]) { pixels++; cnt[((y / CELL) | 0) * gw + ((x / CELL) | 0)]++; }
  const seen = new Uint8Array(gw * gh), out = [];
  for (let g0 = 0; g0 < gw * gh; g0++) {
    if (seen[g0] || cnt[g0] < 2) continue;
    let x0 = gw, y0 = gh, x1 = 0, y1 = 0, n = 0;
    const stack = [g0]; seen[g0] = 1;
    while (stack.length) {
      const g = stack.pop(), gx = g % gw, gy = (g / gw) | 0;
      x0 = Math.min(x0, gx); y0 = Math.min(y0, gy); x1 = Math.max(x1, gx); y1 = Math.max(y1, gy); n += cnt[g];
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        const nx = gx + dx, ny = gy + dy; if (nx < 0 || ny < 0 || nx >= gw || ny >= gh) continue;
        const q = ny * gw + nx; if (!seen[q] && cnt[q] >= 2) { seen[q] = 1; stack.push(q); }
      }
    }
    if (n < 4) continue;
    out.push({ x: x0 * CELL, y: y0 * CELL, w: Math.min(w, (x1 + 1) * CELL) - x0 * CELL, h: Math.min(h, (y1 + 1) * CELL) - y0 * CELL, pixels: n });
  }
  return { regions: out, changed: out.length > 0, pixels };
}

// Run as a Web Worker: post { id, a, b } with two ImageData, get { id, diff } back.
if (typeof window === "undefined" && typeof self !== "undefined") {
  self.onmessage = ({ data: { id, a, b } }) => {
    try { self.postMessage({ id, diff: compare(a, b) }); } catch (e) { self.postMessage({ id, error: String(e && e.message || e) }); }
  };
}
