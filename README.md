# Battaglia Navale – Progetto FSL

Gioco della Battaglia Navale client-server in Python con interfaccia grafica Pygame.

## Requisiti
- Python 3.10 o superiore
- pygame

## Installazione
pip install -r requirements.txt
## Come avviare il gioco
1. Avvia il server su un terminale:
python server.py
2. Avvia il client su due terminali diversi:
python client.py

Se i due PC sono sulla stessa rete locale, modifica la riga
   `SERVER_HOST = "127.0.0.1"` in `client.py` con l'IP del PC server.

## Funzionalità
- Griglia 10×10, flotta standard (5 navi)
- Interfaccia grafica Pygame con coordinate A–J / 1–10
- Chat integrata tra i due giocatori
- Gestione disconnessione improvvisa senza crash
