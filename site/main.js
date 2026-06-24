// Nav background on scroll
const nav = document.getElementById('nav');
const onScroll = () => nav.classList.toggle('scrolled', window.scrollY > 40);
window.addEventListener('scroll', onScroll, { passive: true });
onScroll();

// Run-mode tab switching in the hero terminal
const cmds = {
  install: 'curl -fsSL https://bursar-hermes.com/install.sh | bash',
  run: 'python3 ~/.hermes/plugins/bursar/engine/firehose.py --once 400 --burst'
};
const tabs = document.querySelectorAll('.terminal .tab');
const cmdText = document.getElementById('cmdText');
tabs.forEach(tab => {
  tab.addEventListener('click', () => {
    tabs.forEach(t => t.classList.add('inactive'));
    tab.classList.remove('inactive');
    cmdText.textContent = cmds[tab.dataset.mode];
  });
});

// Copy the run command to clipboard
const copyBtn = document.getElementById('copyBtn');
if (copyBtn) {
  copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(cmdText.textContent);
      const orig = copyBtn.innerHTML;
      copyBtn.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg>';
      setTimeout(() => { copyBtn.innerHTML = orig; }, 1400);
    } catch (e) {}
  });
}

// Hero / CTA buttons jump to the trading floor
const installBtn = document.getElementById('installBtn');
if (installBtn) {
  installBtn.addEventListener('click', () =>
    document.getElementById('floor').scrollIntoView({ behavior: 'smooth' }));
}

// Subtle "clearing the market" flicker on the floor summary once it scrolls in.
// The settled figure ticks up toward the real $41.13; the cut follows.
const billNum = document.getElementById('billNum');
const cutNum = document.getElementById('cutNum');
if (billNum && cutNum && 'IntersectionObserver' in window) {
  const NAIVE = 281.07, FINAL = 41.13;
  let played = false;
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting || played) return;
      played = true;
      const t0 = performance.now(), dur = 1100;
      const step = (now) => {
        const p = Math.min(1, (now - t0) / dur);
        const ease = 1 - Math.pow(1 - p, 3);
        const bill = FINAL + (NAIVE - FINAL) * (1 - ease);
        billNum.textContent = '$' + bill.toFixed(2);
        cutNum.textContent = Math.round((1 - bill / NAIVE) * 100) + '%';
        if (p < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
  }, { threshold: 0.4 });
  io.observe(billNum);
}
