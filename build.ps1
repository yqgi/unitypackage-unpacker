# Builds the Windows release: dist\unitypackage-unpacker\ and dist\unitypackage-unpacker-<version>-win64.zip
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m pip install --upgrade pyinstaller -r requirements.txt
python -m PyInstaller --noconfirm --clean --windowed --onedir --name unitypackage-unpacker --collect-all rapidgzip --exclude-module _ssl --exclude-module ssl --exclude-module _hashlib --exclude-module _socket --exclude-module socket --exclude-module _lzma --exclude-module lzma --exclude-module _bz2 --exclude-module bz2 --exclude-module _decimal --exclude-module decimal --exclude-module _sqlite3 --exclude-module sqlite3 --exclude-module unittest --exclude-module pydoc --exclude-module asyncio --exclude-module multiprocessing unitypackage_unpacker.py
$version = (Select-String -Path unitypackage_unpacker.py -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$zip = "dist\unitypackage-unpacker-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
# Compress-Archive in Windows PowerShell 5.1 writes "\" into entry names, which unzip tools outside Windows keep as part of the name
python -c "import shutil, sys; shutil.make_archive(sys.argv[1], 'zip', 'dist', 'unitypackage-unpacker')" "dist\unitypackage-unpacker-$version-win64"
Write-Host "built $zip"
