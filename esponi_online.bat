@echo off
REM =====================================================================
REM  Mette online il Moto Monitor GRATIS e SENZA REGISTRAZIONE (Windows).
REM  Espone il server che gira sul TUO PC tramite un tunnel pubblico.
REM  Serve Windows 10+ (che include il comando ssh). Ctrl+C per fermare.
REM =====================================================================
cd /d %~dp0

start "Moto Monitor server" python server.py
timeout /t 3 >nul
echo Server locale avviato su http://127.0.0.1:8000 (finestra separata).
echo.
echo Espongo con localhost.run (nessun account, niente da installare).
echo L'URL pubblico compare qui sotto; la vista per l'acquirente e' ^<URL^>/view
echo.
ssh -o StrictHostKeyChecking=accept-new -R 80:localhost:8000 nokey@localhost.run
