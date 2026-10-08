FROM python:3.12-slim

WORKDIR /app

# Installa dipendenze
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Libreria nativa TLS per `tls_requests` (dipendenza di `soccerdata`, usata da
# `live_intel`): la libreria cerca un asset `tls-client-linux-amd64-*.so` che
# nel release v1.13.1 NON esiste (esistono solo le varianti `-ubuntu-`,
# `-alpine-` e `-xgo-`), quindi a runtime ritenta il download a ogni ciclo e
# logga ERROR a ripetizione. La scarichiamo UNA volta a build time nella
# cartella che `tls_requests` scandisce da sola (`TLSLibrary.BIN_DIR`): e'
# sufficiente, perche' il caricatore prende il file locale piu' recente e la
# versione nel nome (1.13.1) coincide col target. Variante `ubuntu` = glibc,
# che e' quella che si carica su questo container Debian (verificato 08/10).
# NON bloccante: se il download fallisce il comportamento resta quello
# precedente (intel degradata, nessun deploy rotto).
RUN python -c "import os,urllib.request,tls_requests.models.libraries as L;u='https://github.com/bogdanfinn/tls-client/releases/download/v1.13.1/tls-client-linux-ubuntu-amd64-1.13.1.so';os.makedirs(L.BIN_DIR,exist_ok=True);f=os.path.join(L.BIN_DIR,'tls-client-linux-ubuntu-amd64-1.13.1.so');urllib.request.urlretrieve(u,f);print('tls-client v1.13.1 ->',f,os.path.getsize(f))" \
    || echo "WARN: download tls-client v1.13.1 fallito (soccerdata restera' degradato, nessun blocco)"

# Copia tutto il codice
COPY . .

# Railway inietta PORT; run_all.py avvia web_api (HTTP) e bot (Telegram)
# nello stesso processo, condividendo un unico volume su /app/data
# (Railway non supporta volumi condivisi fra servizi).
EXPOSE 8000
CMD ["python", "run_all.py"]