#!/bin/bash
# Script de instalación para la VPS gratuita de Oracle Cloud (Ubuntu).
# Ejecutar DESDE DENTRO de la VPS, tras subir el proyecto (ver README de deploy/).
#
# Uso:
#   chmod +x setup_vps.sh
#   ./setup_vps.sh

set -e

PROJECT_DIR="$HOME/wallapop-chollos-bot"

echo "== Instalando Python y dependencias del sistema =="
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

echo "== Creando entorno virtual =="
cd "$PROJECT_DIR"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "== Configurando cron (cada 15 minutos) =="
# flock evita que se solapen dos ciclos si uno tarda más de 15 min (cron, a
# diferencia del Task Scheduler de Windows, no lo evita por sí solo).
CRON_CMD="*/15 * * * * flock -n $PROJECT_DIR/.cycle.lock -c 'cd $PROJECT_DIR && $PROJECT_DIR/.venv/bin/python -m src.main --once' >> $PROJECT_DIR/logs/cron.log 2>&1"
( crontab -l 2>/dev/null | grep -v "wallapop-chollos-bot" ; echo "$CRON_CMD" ) | crontab -

mkdir -p "$PROJECT_DIR/logs"

echo ""
echo "Listo. La tarea cron ya está instalada:"
crontab -l
echo ""
echo "Comprueba que .env tiene TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID rellenados:"
echo "  cat $PROJECT_DIR/.env"
echo ""
echo "Para probar un ciclo manualmente ahora mismo:"
echo "  cd $PROJECT_DIR && ./.venv/bin/python -m src.main --once"
