# OCI 배포 가이드 (24시간 상시 수집)

흐름: **노트북 → GitHub → OCI VM → systemd 상시 구동 → SQLite 적재**

---

## 0. 사전 (노트북, 최초 1회)

```bash
cd "Hynix HTS"
git init
git add collector.py requirements.txt .gitignore deploy/
git commit -m "feat: 폴링 수집기 + 배포 설정"
# GitHub 빈 repo 만든 뒤
git remote add origin git@github.com:<you>/hynix-hts.git
git push -u origin main
```
> `ticks.db`, `.venv/` 는 `.gitignore`로 제외됨 — 코드만 올라감.

---

## 1. OCI VM 준비 (최초 1회)

```bash
ssh opc@<VM_PUBLIC_IP>          # Ubuntu 이미지면 ubuntu@

# Python + git (Oracle Linux 기준; Ubuntu면 apt)
sudo dnf install -y python3 git        # Ubuntu: sudo apt update && sudo apt install -y python3 python3-venv git

git clone https://github.com/<you>/hynix-hts.git ~/hynix-hts
cd ~/hynix-hts
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt
mkdir -p data                          # SQLite 저장 폴더
```

---

## 2. systemd 서비스 등록 (최초 1회)

`deploy/hynix-collector.service` 안의 `User=` / 경로를 VM에 맞게 확인(opc vs ubuntu) 후:

```bash
sudo cp ~/hynix-hts/deploy/hynix-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hynix-collector
```

확인:
```bash
systemctl status hynix-collector          # active (running) 떠야 정상
journalctl -u hynix-collector -f          # 실시간 로그 (수집 라인 흐르는지)
```

---

## 3. 코드 업데이트 시 (반복)

```bash
# 노트북
git push

# VM
cd ~/hynix-hts && git pull
sudo systemctl restart hynix-collector
```

---

## 4. 데이터 확인 / 회수

```bash
# VM 에서 행수 확인
.venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/ticks.db').execute('select count(*) from ticks').fetchone())"

# 노트북으로 DB 내려받기
scp opc@<VM_PUBLIC_IP>:~/hynix-hts/data/ticks.db ./
```

---

## 주의
- **DB는 git에 안 올라감.** VM 디스크에만 쌓임. 정기 백업(scp/오브젝트스토리지) 고려.
- OCI 방화벽: 폴링은 아웃바운드만 쓰므로 인바운드 포트 개방 불필요.
- 디스크 여유 주시 — 2초 간격 3종목이면 하루 약 13만 행(수십 MB/월 수준, 가벼움).
- 차후 브로커 API 키는 `.env`로, **절대 커밋 금지**(.gitignore에 이미 포함).
```
