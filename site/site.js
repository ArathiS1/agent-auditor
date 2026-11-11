// Copy button for code blocks.
(function () {
  // Show the live star count once there are enough stars for it to be useful.
  var star = document.getElementById('stars');
  if (star && window.fetch) {
    fetch('https://api.github.com/repos/ArathiS1/agent-auditor').then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d && d.stargazers_count >= 10) { star.textContent = d.stargazers_count; star.hidden = false; } })
      .catch(function () {});
  }
  document.querySelectorAll('.code button').forEach(function (b) {
    b.addEventListener('click', function () {
      var t = b.parentNode.querySelector('pre').innerText;
      var done = function () { b.textContent = 'Copied'; setTimeout(function () { b.textContent = 'Copy'; }, 1600); };
      if (navigator.clipboard) { navigator.clipboard.writeText(t).then(done, function () {}); }
    });
  });
})();
