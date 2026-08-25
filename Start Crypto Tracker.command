#!/bin/bash
# Double-click this file to start the Crypto Tracker and open it in your browser.
cd "$(dirname "$0")"

if curl -s -o /dev/null --max-time 2 http://127.0.0.1:5178; then
  echo "Crypto Tracker is already running."
else
  echo "Starting Crypto Tracker..."
  nohup ./venv/bin/python app.py > tracker.log 2>&1 &
  for i in $(seq 1 20); do
    sleep 0.5
    curl -s -o /dev/null --max-time 1 http://127.0.0.1:5178 && break
  done
fi

open "http://127.0.0.1:5178"
echo "Crypto Tracker is open in your browser. You can close this window."
echo ""
echo "To view from other devices on your home network, go to:"
echo "    http://$(scutil --get LocalHostName | tr '[:upper:]' '[:lower:]').local:5178"
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
[ -n "$IP" ] && echo "    (or http://$IP:5178)"
