// pm2 ecosystem template for running the EvoClaw host process under pm2.
//
// Setup:
//   1. cp ecosystem.config.example.js ecosystem.config.js
//   2. Adjust `interpreter` if your Python is not on PATH (e.g. point at a venv)
//   3. pm2 start ecosystem.config.js
//   4. pm2 save                  # persist for restart
//
// Boot-time resurrection:
//   - Windows: npm install -g pm2-windows-startup && pm2-startup install
//   - Linux/macOS: pm2 startup    (then run the sudo command it prints)
//
// The local `ecosystem.config.js` is gitignored so each environment can pin
// its own absolute paths without polluting the repo.

module.exports = {
  apps: [
    {
      name: "evoclaw",
      script: "run.py",
      // Path to the Python interpreter. "python" looks it up on PATH; use an
      // absolute path to pin a specific venv or conda env, e.g.
      //   "/home/me/.venvs/evoclaw/bin/python"
      //   "C:/Users/me/miniconda3/python.exe"
      interpreter: "python",
      cwd: __dirname,
      watch: false,
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 10,
      min_uptime: "10s",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      out_file: "logs/pm2-out.log",
      error_file: "logs/pm2-err.log",
      merge_logs: true,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
