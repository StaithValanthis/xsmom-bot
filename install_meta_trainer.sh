#!/usr/bin/env bash
# scripts/install_meta_trainer.sh
set -euo pipefail

ROOT="/opt/xsmom-bot"
SRC_DIR="$ROOT/src"

install -d "$SRC_DIR"
cp -f "/mnt/data/src/meta_label_trainer.py" "$SRC_DIR/meta_label_trainer.py"
cp -f "/mnt/data/src/optimizer_bayes.py"     "$SRC_DIR/optimizer_bayes.py"

sudo touch /var/log/xsmom-meta-trainer.log
sudo chown ubuntu:ubuntu /var/log/xsmom-meta-trainer.log || true

sudo cp -f "/mnt/data/systemd/xsmom-meta-trainer.service" /etc/systemd/system/xsmom-meta-trainer.service
sudo cp -f "/mnt/data/systemd/xsmom-meta-trainer.timer"   /etc/systemd/system/xsmom-meta-trainer.timer

sudo systemctl daemon-reload
sudo systemctl enable --now xsmom-meta-trainer.timer

echo "Installed. Next runs:"
systemctl list-timers | grep xsmom-meta-trainer || true

echo
echo "Manual run:"
echo "  sudo systemctl start xsmom-meta-trainer.service && tail -n 200 -f /var/log/xsmom-meta-trainer.log"

echo
echo "Run the Bayesian optimizer wrapper once:"
echo "  PYTHONPATH=$ROOT $ROOT/venv/bin/python3 -m src.optimizer_bayes \\"
echo "    --config $ROOT/config/config.yaml --objective sharpe --splits 3 --embargo 0.02 --max-symbols 10 \\"
echo "    --space '{\"k\":[2,12],\"gross\":[0.6,1.6]}' --init 8 --iters 24"
