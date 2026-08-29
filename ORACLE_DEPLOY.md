# Oracle Cloud 永久無料枠へのデプロイ手順

Oracle Cloudの「Always Free（永久無料枠）」を使い、追加料金が一切発生しない状態で
このアプリをインターネット上に公開し、iPhoneから使えるようにする手順です。

**重要**: アカウントを「Pay As You Go（従量課金）」にアップグレードしない限り、
Always Free枠を超える操作はエラーになるだけで、勝手に課金されることはありません。
アップグレードを促す画面が出ても進めなければ安全です。

---

## 1. Oracle Cloudアカウント作成

1. https://www.oracle.com/cloud/free/ にアクセスし「Start for free」
2. メールアドレス・氏名・国（Taiwan等）を入力
3. 本人確認のためクレジットカード情報の登録が必須（Always Free枠のみの利用なら課金されません）
4. アカウント作成完了後、Oracle Cloud コンソールにログイン

## 2. インスタンス（VPS本体）の作成

1. コンソール左上のメニュー →「Compute」→「Instances」→「Create Instance」
2. **Name**: 好きな名前（例: `scraper-app`）
3. **Image and shape**:
   - Image: `Ubuntu 22.04`
   - Shape: 「Change shape」→ **Ampere (Arm)** の `VM.Standard.A1.Flex` が選べれば
     OCPU数=2、メモリ=12GB程度に設定（永久無料枠内、在庫があれば）。
     選べない・在庫エラーが出る場合は **Always Free eligible** と表示される
     `VM.Standard.E2.1.Micro`（AMD, 1 OCPU / 1GB RAM）を選択（こちらは確実に作成できます）。
4. **Networking**: デフォルトのVCNのままでOK（「Assign a public IPv4 address」がオンになっていることを確認）
5. **Add SSH keys**: 「Generate a key pair for me」を選び、秘密鍵（`.key`ファイル）をダウンロード
   （このファイルがないとSSH接続できないので大切に保管してください）
6. 「Create」をクリックしてインスタンス作成（数分で起動）
7. 作成後、インスタンス詳細画面に表示される **Public IP Address** を控えておく

## 3. ポートの開放（Oracle Cloud側のファイアウォール）

1. インスタンス詳細画面 →「Virtual cloud network」のリンクをクリック
2. 「Security Lists」→ デフォルトのセキュリティリストを開く
3. 「Add Ingress Rules」で以下を追加:
   - Source CIDR: `0.0.0.0/0`
   - IP Protocol: TCP
   - Destination Port Range: `8550`
   （後でHTTPS化する場合は `443` も同様に追加）

## 4. SSH接続してDocker環境を用意

ダウンロードした秘密鍵を使って接続します（Mac/Linuxならターミナル、Windowsなら
PowerShellかWSL、またはTeraTerm等を使用）。

```bash
chmod 600 ~/Downloads/ssh-key-xxxx.key
ssh -i ~/Downloads/ssh-key-xxxx.key ubuntu@<Public IP Address>
```

接続できたら、Dockerをインストールします。

```bash
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# 一度ログアウトし、再度SSH接続し直す（グループ反映のため）
exit
```

再接続後:

```bash
ssh -i ~/Downloads/ssh-key-xxxx.key ubuntu@<Public IP Address>
docker --version   # 動作確認
```

### 1GBメモリの場合はスワップ領域を追加（重要）

`VM.Standard.E2.1.Micro`（1GB RAM）を選んだ場合、Chromiumがメモリ不足で
落ちることがあるため、スワップを追加しておきます（Ampere A1で12GB等を確保できた場合は不要）。

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### OS側のファイアウォールも開放

Oracle CloudのUbuntuイメージは `iptables` で追加のフィルタがかかっていることがあります。

```bash
sudo iptables -I INPUT -p tcp --dport 8550 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true
```

## 5. アプリのアップロードと起動

ローカルPCから、アプリ一式（このzipの中身）をサーバーに転送します。

```bash
# ローカルPC側で実行（zipファイルがある場所で）
scp -i ~/Downloads/ssh-key-xxxx.key scraper_app.zip ubuntu@<Public IP Address>:~
```

サーバー側で展開してビルド・起動:

```bash
sudo apt install -y unzip
unzip scraper_app.zip -d scraper_app
cd scraper_app

docker build -t scraper-app .

docker run -d \
  --name scraper-app \
  --restart unless-stopped \
  -p 8550:8550 \
  -e SERPER_API_KEY="Serper.devで取得したAPIキー" \
  -e APP_PASSWORD="お好きなパスワードに変更してください" \
  -v $(pwd)/site_configs:/app/site_configs \
  scraper-app
```

`SERPER_API_KEY` は README.md の「事前準備」の手順で取得してください
（https://serper.dev/ ）。設定しないと検索機能が動作しません。

- `--restart unless-stopped`: サーバー再起動時もアプリが自動起動します
- `-v $(pwd)/site_configs:/app/site_configs`: サイト設定をサーバーのディスクに永続化
  （コンテナを作り直してもサイト設定が消えません。無料枠のディスクがある限り有効です）

## 6. 動作確認

ブラウザ（PCでもiPhoneでも）で以下にアクセス:

```
http://<Public IP Address>:8550
```

パスワード入力画面が出れば成功です。`APP_PASSWORD` に設定した文字列でログインできます。

iPhoneのSafariでこのURLを開き、共有メニュー→「ホーム画面に追加」しておくと、
アプリのようにワンタップで開けます。

## 7. （任意）常時 https 化したい場合

IPアドレス直打ち・素のHTTPでも動作はしますが、気になる場合は無料のドメイン
（[DuckDNS](https://www.duckdns.org/) 等）を取得し、[Caddy](https://caddyserver.com/) を
リバースプロキシとして立てるとLet's Encryptで自動的にHTTPS化できます。必要であれば
別途手順をご案内します。

## トラブルシューティング

| 症状 | 確認ポイント |
|---|---|
| ブラウザで繋がらない | Oracle Cloud側の Security List、OS側の iptables、両方でポート8550を開けているか |
| スクレイピングが固まる/落ちる | `docker logs scraper-app` でエラー確認。1GBインスタンスならスワップ設定を確認 |
| サーバー再起動後アプリが消えた | `docker ps` でコンテナ起動を確認。`--restart unless-stopped` 付きで起動し直す |
| コンテナを更新したい | `docker stop scraper-app && docker rm scraper-app` 後、`docker build` からやり直す |
