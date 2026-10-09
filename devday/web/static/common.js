// Shared helpers for the upload and result pages.
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmtBytes(n) {
  if (n > 1e9) return (n / 1e9).toFixed(1) + 'GB';
  if (n > 1e6) return (n / 1e6).toFixed(1) + 'MB';
  return Math.round(n / 1e3) + 'KB';
}
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function fmtNum(v, digits = 3) {
  if (v === null || v === undefined) return '–';
  const a = Math.abs(v);
  if (a !== 0 && a < 0.01) return v.toFixed(5);
  if (a >= 100) return v.toFixed(0);
  return Number(v.toFixed(digits)).toString();
}
const LABEL_KO = { abnormal: '비정상', normal: '정상', insufficient: '판단 불가' };
function stateBadge(state, label) {
  if (state === 'done') return `<span class="badge ${label}">${LABEL_KO[label] || '완료'}</span>`;
  if (state === 'failed') return '<span class="badge failed">실패</span>';
  if (state === 'running') return '<span class="badge running">분석 중</span>';
  return '<span class="badge queued">대기 중</span>';
}
