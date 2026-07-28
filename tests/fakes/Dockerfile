# Obraz atrapy gniazdka Meross. Kontekst budowania: tests/fakes/
FROM python:3.12-slim

# Wersja aiohttp jak w tests/requirements-dev.txt, żeby atrapa i testy stały na tym samym.
RUN pip install --no-cache-dir aiohttp==3.14.3

WORKDIR /app
COPY fake_device.py /app/fake_device.py

# Atrapa słucha na porcie 80 w kontenerze, tak jak prawdziwe gniazdko w LAN-ie.
EXPOSE 80

HEALTHCHECK --interval=5s --timeout=3s --retries=10 --start-period=3s \
    CMD python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:80/debug/state', timeout=2)" || exit 1

ENTRYPOINT ["python3", "/app/fake_device.py"]
