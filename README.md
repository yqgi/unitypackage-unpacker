# unitypackage unpacker

[English](README.en.md)

`.unitypackage`をエクスプローラーの右クリックメニューから展開するツールです。Unityにインポートしたときと同じフォルダ構成とファイル名で書き出します。

`.unitypackage`を7-Zipなどで開くと、GUIDを名前にしたフォルダが並ぶだけで、どれが何のファイルなのか分かりません。各フォルダには元のパスを記録した`pathname`というファイルが入っています。このツールはそれを読み取り、各ファイルを元のパスに配置します。

![進行状況のウィンドウ](docs/progress.png)

## 特徴

- ネットワークには一切接続しません。このツールには通信の処理がないので、展開したファイルの名前などが外部に送られることはありません。
- 本体はPythonのファイル1つ（約780行）で、標準ライブラリだけで動きます。コードを全部読んで、動作を確かめられます。
- 設定項目はありません。右クリックして選べば展開が始まり、終わっても完了のメッセージは出ません。

## 使い方

`.unitypackage`を右クリックして「unpack .unitypackage」を選んでください。パッケージと同じ場所に同名のフォルダが作られ、Unityのプロジェクトと同じ構成でファイルが展開されます。`.meta`ファイルも含まれます。

Windows 11の場合、表示場所はメニューの「その他のオプションを確認」の中です。Shiftキーを押しながら右クリックすれば最初から表示されます。ドラッグ＆ドロップで展開したいときは、パッケージをexeファイルにドロップしてください。

大きなパッケージで展開に時間がかかるときは、進行状況のウィンドウが開き、終わると自動で閉じます。展開先のフォルダの外を指すパスが含まれていた場合、そのファイルはスキップします。

## インストール

1. [Releases](https://github.com/yqgi/unitypackage-unpacker/releases)から`unitypackage-unpacker-<バージョン>-win64.zip`をダウンロードする
2. zipを任意の場所に展開する  
   例 : `%LOCALAPPDATA%\Programs\unitypackage-unpacker`
3. `unitypackage-unpacker.exe`を起動して「Install」を押す

右クリックメニューから呼び出されるのは、展開したフォルダ内の`unitypackage-unpacker.exe`です。フォルダを削除するとメニューが使えなくなるため、展開したフォルダは残しておいてください。フォルダを移動した場合は、移動先でexeを起動して、もう一度「Install」を押してください。

メニューは現在のユーザーにだけ追加されるので、管理者権限は不要です。

exeにはコード署名をしていないため、初回の起動時にWindows SmartScreenの警告が表示されることがあります。その場合は「詳細情報」を押してから「実行」を選んでください。PyInstallerで作成したexeは、ウイルス対策ソフトに誤検知されることもあります。気になる場合は、ソースコードから実行するか、ご自身でビルドしてください。

## アンインストール

`unitypackage-unpacker.exe`を起動して「Uninstall」を押してから、フォルダごと削除してください。

## ソースコードから実行する

Windows上のPython 3.10以降が必要です。`rapidgzip`がない環境では標準のgzipモジュールで展開するので、大きなパッケージでは少し時間がかかります。

```
pip install -r requirements.txt
python unitypackage_unpacker.py --install
```

コマンドラインから実行した場合、展開の結果はパッケージごとにJSON形式で1行ずつ出力されます。進行状況のウィンドウを表示したいときは`--gui`を付けてください。

```
python unitypackage_unpacker.py <file.unitypackage> ... [--out DIR] [--no-meta] [--gui]
python unitypackage_unpacker.py --install
python unitypackage_unpacker.py --uninstall
```

## ビルド

```
powershell -ExecutionPolicy Bypass -File build.ps1
```

PyInstallerで`dist\unitypackage-unpacker\`と配布用のzipを作成します。テストは`python -m unittest discover -s tests`で実行できます。

## ライセンス

MIT
