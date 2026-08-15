# Desplegar en una VPS gratuita (Oracle Cloud Always Free)

## 0. Requisitos previos

- Cuenta de Oracle Cloud creada y una instancia Ubuntu (Ampere/ARM) ya
  levantada, con su **IP pública** y la **clave SSH privada** descargada.

## 1. Copiar el proyecto a la VPS

Desde tu PC (en Git Bash, misma terminal que usamos hasta ahora), sustituyendo
`<IP>` por la IP pública de la instancia y `<clave.pem>` por la ruta a la
clave SSH descargada:

```bash
chmod 600 <clave.pem>   # SSH exige permisos restrictivos en la clave
scp -i <clave.pem> -r "C:/Users/34644/Downloads/Proyectos/wallapop-chollos-bot" ubuntu@<IP>:~/
```

Esto copia también `.env` (con tus credenciales de Telegram) y `data/wallapop.db`
(el histórico ya acumulado) — así no empiezas de cero.

## 2. Conectarte y ejecutar el instalador

```bash
ssh -i <clave.pem> ubuntu@<IP>
cd wallapop-chollos-bot
chmod +x deploy/setup_vps.sh
./deploy/setup_vps.sh
```

El script instala Python, crea el entorno virtual, instala las dependencias
y deja programado un cron cada 15 minutos (con protección `flock` para que
nunca se solapen dos ciclos).

## 3. Verificar que funciona

```bash
# Ciclo manual de prueba
cd ~/wallapop-chollos-bot && ./.venv/bin/python -m src.main --once

# Ver el log en vivo
tail -f ~/wallapop-chollos-bot/logs/bot.log

# Ver la tarea cron instalada
crontab -l
```

Deberías recibir el heartbeat de ciclo en Telegram igual que en Windows.

## 4. Pasos siguientes (opcional pero recomendado)

- **Apaga la tarea programada de Windows** (`Disable-ScheduledTask -TaskName
  "WallapopChollosBot"`) para no duplicar ciclos si alguna vez enciendes el
  PC con la config antigua.
- Guarda la clave SSH en un sitio seguro — sin ella no puedes volver a
  entrar a la VPS.
- Si quieres actualizar el código más adelante, repite el `scp` del paso 1
  (sobrescribe los archivos, no toca `data/wallapop.db` si no lo incluyes
  de nuevo) y reinicia si has tocado dependencias:
  `./.venv/bin/pip install -r requirements.txt`.
