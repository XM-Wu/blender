@echo off
setlocal
set "XBONE_EXE=%~dp0..\build_xbone_guide\bin\blender.exe"
if not exist "%XBONE_EXE%" (
  echo XBone-enabled Blender build missing: "%XBONE_EXE%"
  pause
  exit /b 1
)
if "%~1"=="" (
  if exist "%~dp0..\artifacts\xbone-dynamics\XBone-Ponytail.blend" (
    "%XBONE_EXE%" --factory-startup "%~dp0..\artifacts\xbone-dynamics\XBone-Ponytail.blend" --python "%~dp0scripts\startup\xbone_dynamics.py"
  ) else (
    "%XBONE_EXE%" --factory-startup --python "%~dp0tests\python\bl_xbone_dynamics.py" -- --demo
  )
) else (
  "%XBONE_EXE%" --factory-startup "%~f1" --python "%~dp0scripts\startup\xbone_dynamics.py"
)
