// bud content script — inject floating "Ask bud" button on text selection
const BUD_URL = 'http://127.0.0.1:8082';

let _bubble = null;

function removeBubble() {
  if (_bubble) { _bubble.remove(); _bubble = null; }
}

document.addEventListener('mouseup', (e) => {
  removeBubble();
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.toString().trim().length < 3) return;

  const text = sel.toString().trim();
  const range = sel.getRangeAt(0);
  const rect  = range.getBoundingClientRect();

  _bubble = document.createElement('div');
  _bubble.textContent = '⚡ Ask bud';
  Object.assign(_bubble.style, {
    position:   'fixed',
    top:        (rect.top - 38 + window.scrollY) + 'px',
    left:       Math.max(0, rect.left + rect.width / 2 - 50) + 'px',
    zIndex:     '2147483647',
    background: '#1a1815',
    color:      '#c8a45e',
    border:     '1px solid #c8a45e55',
    borderRadius:'20px',
    padding:    '5px 14px',
    fontSize:   '13px',
    fontFamily: 'system-ui, sans-serif',
    fontWeight: '600',
    cursor:     'pointer',
    boxShadow:  '0 4px 20px rgba(0,0,0,0.5)',
    userSelect: 'none',
    transition: 'transform 0.1s',
    transform:  'translateY(4px)',
  });

  _bubble.addEventListener('mouseenter', () => _bubble.style.transform = 'translateY(0)');
  _bubble.addEventListener('mouseleave', () => _bubble.style.transform = 'translateY(4px)');
  _bubble.addEventListener('click', () => {
    const url = `${BUD_URL}/?ask=${encodeURIComponent(text)}&source=${encodeURIComponent(location.href)}`;
    window.open(url, '_blank');
    removeBubble();
  });

  document.body.appendChild(_bubble);
});

document.addEventListener('mousedown', (e) => {
  if (_bubble && !_bubble.contains(e.target)) removeBubble();
});
