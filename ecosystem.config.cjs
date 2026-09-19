// PM2 config for local/sandbox preview of the VLESS Forge UI
module.exports = {
  apps: [
    {
      name: 'vless-ui',
      script: 'ui_server.py',
      interpreter: 'python3',
      cwd: __dirname,
      env: { UI_PORT: 8000, OUT_DIR: __dirname + '/out', PYTHONUNBUFFERED: '1' },
      watch: false,
      instances: 1,
      exec_mode: 'fork'
    }
  ]
}
