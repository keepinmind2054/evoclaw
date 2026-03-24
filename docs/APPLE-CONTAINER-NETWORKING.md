# Apple Container 網路設定（macOS 26）

Apple Container 的 vmnet 網路需要手動設定，容器才能存取網際網路。若未進行設定，容器可以與主機通訊，但無法連線至外部服務（DNS、HTTPS、API）。

## 快速設定

執行以下兩個指令（需要 `sudo`）：

```bash
# 1. 啟用 IP 轉送，讓主機路由容器流量
sudo sysctl -w net.inet.ip.forwarding=1

# 2. 啟用 NAT，讓容器流量透過你的網際網路介面進行偽裝
echo "nat on en0 from 192.168.64.0/24 to any -> (en0)" | sudo pfctl -ef -
```

> **注意：** 請將 `en0` 替換為你的實際網際網路介面。可用以下指令確認：`route get 8.8.8.8 | grep interface`

## 設定持久化

上述設定在重新開機後會被重置。若要使其永久生效：

**IP 轉送** — 加入至 `/etc/sysctl.conf`：
```
net.inet.ip.forwarding=1
```

**NAT 規則** — 加入至 `/etc/pf.conf`（放在現有規則之前）：
```
nat on en0 from 192.168.64.0/24 to any -> (en0)
```

然後重新載入：`sudo pfctl -f /etc/pf.conf`

## IPv6 DNS 問題

預設情況下，DNS 解析器會優先回傳 IPv6（AAAA）記錄，而非 IPv4（A）記錄。由於我們的 NAT 僅處理 IPv4，容器內的 Node.js 應用程式會先嘗試 IPv6 並失敗。

容器映像檔與執行器已透過以下設定優先使用 IPv4：
```
NODE_OPTIONS=--dns-result-order=ipv4first
```

此設定已寫入 `Dockerfile`，並在 `container-runner.ts` 中透過 `-e` 旗標傳入。

## 驗證

```bash
# 確認 IP 轉送已啟用
sysctl net.inet.ip.forwarding
# 預期輸出：net.inet.ip.forwarding: 1

# 測試容器是否能存取網際網路
container run --rm --entrypoint curl evoclaw-agent:latest \
  -s4 --connect-timeout 5 -o /dev/null -w "%{http_code}" https://generativelanguage.googleapis.com
# 預期輸出：404

# 確認橋接介面（僅在容器執行中時存在）
ifconfig bridge100
```

## 疑難排解

| 症狀 | 原因 | 修復方式 |
|------|------|----------|
| `curl: (28) Connection timed out` | IP 轉送已停用 | `sudo sysctl -w net.inet.ip.forwarding=1` |
| HTTP 正常，HTTPS 逾時 | IPv6 DNS 解析 | 加入 `NODE_OPTIONS=--dns-result-order=ipv4first` |
| `Could not resolve host` | DNS 未轉送 | 確認 bridge100 存在，驗證 pfctl NAT 規則 |
| 容器在輸出後卡住 | agent-runner 缺少 `process.exit(0)` | 重新建置容器映像檔 |

## 運作原理

```
Container VM (192.168.64.x)
    │
    ├── eth0 → gateway 192.168.64.1
    │
bridge100 (192.168.64.1) ← 主機橋接，由 vmnet 在容器執行時建立
    │
    ├── IP 轉送（sysctl）將封包從 bridge100 路由至 en0
    │
    ├── NAT（pfctl）將 192.168.64.0/24 偽裝為 en0 的 IP
    │
en0（你的 WiFi/乙太網路）→ 網際網路
```

## 參考資料

- [apple/container#469](https://github.com/apple/container/issues/469) — 在 macOS 26 上容器無法連線網路
- [apple/container#656](https://github.com/apple/container/issues/656) — 建置期間無法存取網際網路 URL
