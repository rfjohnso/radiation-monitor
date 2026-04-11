@echo off
REM Start the Radiacode-HA bridge silently using pythonw (no console window)
cd /d %~dp0
%USERPROFILE%\miniconda3\pythonw.exe radiacode_ha.py
