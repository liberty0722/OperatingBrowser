@echo off
cd /d %~dp0
echo Starting AI Browser Controller...
echo Open http://localhost:5000 in your browser
uv run python app.py
pause
