"""
server.py – Battaglia Navale: Server TCP
========================================
Il server gestisce la partita tra due client:
  - Accetta esattamente 2 connessioni TCP
  - Coordina il posizionamento delle navi
  - Gestisce i turni (chi spara, chi aspetta)
  - Verifica colpi (Acqua / Colpito / Affondato)
  - Rileva la condizione di vittoria
  - Invia/riceve messaggi di chat
  - Gestisce la disconnessione improvvisa senza crash
    (thread separato per ogni client → nessun blocco)
"""

import socket
import threading
import json
import sys

# ─────────────────────────────────────────────
# CONFIGURAZIONE
# ─────────────────────────────────────────────
HOST      = "127.0.0.1"   # Ascolta su tutte le interfacce
PORT      = 50016        # Porta TCP
GRID_SIZE = 10           # Griglia 10x10

# Flotta standard Battaglia Navale: {nome: lunghezza}
FLEET = {
    "Portaerei":          5,
    "Corazzata":          4,
    "Incrociatore":       3,
    "Sottomarino":        3,
    "Cacciatorpediniere": 2,
}


# ─────────────────────────────────────────────
# STATO GLOBALE DELLA PARTITA
# ─────────────────────────────────────────────
clients   = [None, None]       # socket dei 2 giocatori
grids     = [None, None]       # griglie ricevute dai giocatori
lock      = threading.Lock()   # mutex per stato condiviso
turn      = 0                  # indice del giocatore di turno (0 o 1)
game_over = False              # flag: partita terminata

# Evento usato per svegliare il loop di gioco quando arriva un colpo
shot_event  = threading.Event()
shot_queue  = []               # coda dei messaggi ricevuti (max 1 elemento)


# ─────────────────────────────────────────────
# FUNZIONI DI COMUNICAZIONE
# ─────────────────────────────────────────────

def send_msg(sock, msg: dict):
    """
    Serializza msg come JSON e lo invia sul socket.
    Usa '\n' come delimitatore di messaggio per facilitare
    la lettura lato client (protocollo line-delimited JSON).
    """
    try:
        data = json.dumps(msg) + "\n"
        sock.sendall(data.encode("utf-8"))
    except Exception as e:
        print(f"[SERVER] Errore invio: {e}")


def recv_msg(sock) -> dict | None:
    """
    Legge dati dal socket finché trova un '\n',
    poi deserializza il JSON ricevuto.
    Restituisce None in caso di errore o connessione chiusa.
    """
    buffer = ""
    try:
        while True:
            chunk = sock.recv(4096).decode("utf-8")
            if not chunk:
                return None          # connessione chiusa dal client
            buffer += chunk
            if "\n" in buffer:
                line, _ = buffer.split("\n", 1)
                return json.loads(line)
    except Exception:
        return None


def broadcast(msg: dict, exclude: int = -1):
    """
    Invia msg a entrambi i client, escludendo facoltativamente
    il giocatore con indice 'exclude'.
    """
    for i, client in enumerate(clients):
        if i != exclude and client is not None:
            send_msg(client, msg)


# ─────────────────────────────────────────────
# LOGICA DI GIOCO
# ─────────────────────────────────────────────

def parse_grid(raw_grid: list) -> tuple:
    """
    Converte la griglia (lista di liste interi) ricevuta dal client in:
      - cells: dict {(r,c): int}  dove 0=acqua, intero>0=id nave
      - ships: dict {id_nave: set((r,c),...)}  per verificare affondamento
    """
    cells = {}
    ships = {}
    for r, row in enumerate(raw_grid):
        for c, val in enumerate(row):
            cells[(r, c)] = val
            if val > 0:
                ships.setdefault(val, set()).add((r, c))
    return cells, ships


def check_shot(cells: dict, ships: dict, hits: set, r: int, c: int) -> str:
    """
    Valuta il colpo alle coordinate (r, c) sulla griglia del difensore:
      - "ACQUA"     → la cella non contiene una nave
      - "COLPITO"   → nave colpita ma non ancora completamente affondata
      - "AFFONDATO" → tutti i segmenti della nave sono stati colpiti
    Aggiorna il set 'hits' aggiungendo (r,c) se è una nave.
    """
    if cells.get((r, c), 0) == 0:
        return "ACQUA"

    hits.add((r, c))

    ship_id    = cells[(r, c)]
    ship_cells = ships[ship_id]
    if ship_cells.issubset(hits):
        return "AFFONDATO"
    return "COLPITO"


def all_sunk(ships: dict, hits: set) -> bool:
    """
    Restituisce True se tutte le navi del difensore sono state affondate,
    ovvero ogni cella occupata da una nave è presente nel set hits.
    """
    all_cells = set()
    for sc in ships.values():
        all_cells |= sc
    return all_cells.issubset(hits)


# ─────────────────────────────────────────────
# GESTIONE DISCONNESSIONE
# ─────────────────────────────────────────────

def handle_disconnect(disconnected_idx: int):
    """
    Chiamata quando un client si disconnette inaspettatamente
    (in qualsiasi fase: posizionamento, attesa, turno di gioco).
    - Imposta game_over per fermare tutti i loop
    - Notifica l'altro giocatore con un messaggio chiaro
    - Chiude entrambe le connessioni in modo pulito
    - Sblocca shot_event per evitare che il game_loop rimanga bloccato
    """
    global game_over
    with lock:
        if game_over:
            return          # già gestito, non fare nulla
        game_over = True

    print(f"[SERVER] Giocatore {disconnected_idx + 1} disconnesso.")

    other = 1 - disconnected_idx
    if clients[other] is not None:
        send_msg(clients[other], {
            "type":   "disconnect",
            "winner": other,          # l'altro vince per abbandono
            "msg":    "L'avversario si e' disconnesso. Hai vinto per abbandono!"
        })

    # Sblocca il game_loop se è in attesa di un colpo
    shot_event.set()

    # Chiudi entrambi i socket
    for i, c in enumerate(clients):
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            clients[i] = None


# ─────────────────────────────────────────────
# THREAD LISTENER PER OGNI CLIENT
# ─────────────────────────────────────────────

def client_listener(player_idx: int):
    """
    Thread dedicato ad ascoltare i messaggi in arrivo da un singolo client.
    Gestisce:
      - Messaggi di chat: inoltrati immediatamente all'altro giocatore
      - Messaggi di tiro (shot): messi in coda e segnalati al game_loop
      - Disconnessione: rilevata e gestita senza bloccare il server
    Avere un thread per client permette di ricevere chat e colpi
    in qualsiasi momento, anche quando l'altro client è di turno.
    """
    global game_over
    sock = clients[player_idx]

    while not game_over:
        msg = recv_msg(sock)

        if msg is None:
            # recv_msg restituisce None → client disconnesso
            handle_disconnect(player_idx)
            return

        mtype = msg.get("type")

        if mtype == "chat":
            # Messaggio di chat: inoltrato a entrambi i giocatori
            broadcast(
                {"type": "chat", "from": player_idx, "text": msg.get("text", "")},
                exclude=-1
            )

        elif mtype == "shot":
            # Colpo: accettato solo se è il turno di questo giocatore
            with lock:
                is_my_turn = (turn == player_idx) and not game_over
            if is_my_turn:
                with lock:
                    shot_queue.append(msg)
                shot_event.set()    # sblocca il game_loop


# ─────────────────────────────────────────────
# LOOP PRINCIPALE DI PARTITA
# ─────────────────────────────────────────────

def game_loop():
    """
    Coordina la partita dopo che entrambi i giocatori hanno posizionato
    le navi. Gira in loop finché non c'è un vincitore o una disconnessione:
      1. Aspetta un colpo dal giocatore di turno (via shot_event)
      2. Verifica il risultato (Acqua / Colpito / Affondato)
      3. Notifica entrambi i giocatori del risultato
      4. Controlla la condizione di vittoria
      5. Se Acqua → passa il turno; altrimenti il tiratore gioca ancora
    """
    global turn, game_over

    cells = [None, None]
    ships = [None, None]
    hits  = [set(), set()]     # hits[i] = celle colpite della griglia i

    cells[0], ships[0] = parse_grid(grids[0])
    cells[1], ships[1] = parse_grid(grids[1])

    # Comunica a entrambi l'inizio della partita
    send_msg(clients[0], {
        "type": "start", "your_turn": True,
        "msg":  "Partita iniziata! Tocca a te sparare."
    })
    send_msg(clients[1], {
        "type": "start", "your_turn": False,
        "msg":  "Partita iniziata! Aspetta il tuo turno."
    })

    while not game_over:
        shot_event.clear()
        shot_event.wait()           # attende finché arriva un colpo o disconnect

        if game_over:
            return                  # disconnessione rilevata dal listener

        with lock:
            if not shot_queue:
                continue
            msg      = shot_queue.pop(0)
            attacker = turn
            defender = 1 - turn

        r, c   = msg["row"], msg["col"]
        result = check_shot(cells[defender], ships[defender], hits[defender], r, c)

        broadcast({
            "type":     "shot_result",
            "row":      r,
            "col":      c,
            "result":   result,
            "attacker": attacker,
        })

        if all_sunk(ships[defender], hits[defender]):
            game_over = True
            broadcast({"type": "game_over", "winner": attacker})
            print(f"[SERVER] Giocatore {attacker + 1} ha vinto!")
            return

        # Acqua → passa il turno; Colpito/Affondato → stesso giocatore
        if result == "ACQUA":
            with lock:
                turn = 1 - turn


# ─────────────────────────────────────────────
# RICEZIONE GRIGLIA DI POSIZIONAMENTO
# ─────────────────────────────────────────────

def recv_placement(player_idx: int, result_box: list):
    """
    Riceve dal client player_idx il messaggio "placement" contenente
    la griglia con le navi posizionate.
    Scrive il risultato in result_box[0]; None in caso di errore.
    """
    while True:
        msg = recv_msg(clients[player_idx])
        if msg is None:
            result_box[0] = None
            return
        if msg.get("type") == "chat":
            # Può capitare di ricevere chat anche durante il posizionamento
            broadcast({"type": "chat", "from": player_idx,
                       "text": msg.get("text", "")}, exclude=-1)
            continue
        if msg.get("type") == "placement":
            result_box[0] = msg["grid"]
            return
        # Tipo sconosciuto: ignora e riprova
        result_box[0] = None
        return


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    """
    Avvia il server TCP:
      1. Accetta 2 connessioni
      2. Raccoglie le griglie di posizionamento in parallelo
      3. Avvia un thread listener per ogni client
      4. Esegue il loop di gioco nel thread principale
      5. Chiude tutto alla fine
    """
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR evita "Address already in use" al riavvio rapido
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((HOST, PORT))
    server_sock.listen(2)
    print(f"[SERVER] In ascolto su {HOST}:{PORT} ...")

    for i in range(2):
        conn, addr = server_sock.accept()
        clients[i] = conn
        print(f"[SERVER] Giocatore {i + 1} connesso da {addr}")
        send_msg(conn, {
            "type":      "welcome",
            "player_id": i,
            "grid_size": GRID_SIZE,
            "fleet":     FLEET
        })

    print("[SERVER] Entrambi connessi. Attendo le griglie...")

    # Raccoglie le griglie di posizionamento da entrambi i client in parallelo
    results = [[None], [None]]
    threads = []
    for i in range(2):
        t = threading.Thread(target=recv_placement, args=(i, results[i]), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    for i in range(2):
        if results[i][0] is None:
            print(f"[SERVER] Giocatore {i+1}: griglia non valida o disconnessione.")
            for c in clients:
                if c:
                    try:
                        c.close()
                    except Exception:
                        pass
            sys.exit(1)

    grids[0] = results[0][0]
    grids[1] = results[1][0]
    print("[SERVER] Griglie ricevute. Avvio partita!")

    # Avvia un thread listener per ogni client (chat + colpi + disconnect)
    for i in range(2):
        lt = threading.Thread(target=client_listener, args=(i,), daemon=True)
        lt.start()

    game_loop()

    # Chiude i socket rimasti aperti
    for c in clients:
        if c:
            try:
                c.close()
            except Exception:
                pass
    server_sock.close()
    print("[SERVER] Partita terminata. Server chiuso.")


if __name__ == "__main__":
    main()