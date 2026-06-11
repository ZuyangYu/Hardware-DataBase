@echo off
cd /d "%~dp0"
"C:\Users\renfeng_zhang\workspace\.conda_envs\hardware_database\python.exe" -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
pause
