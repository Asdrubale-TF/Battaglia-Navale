"""
client.py – Battaglia Navale: Client TCP con GUI Pygame
========================================================
Layout a due colonne compatto (~860x620 px):
  Colonna sinistra  → le due griglie di gioco affiancate
  Colonna destra    → pannello info/stato + chat
Gestisce:
  - Connessione TCP al server
  - Fase posizionamento navi (click + R per ruotare)
  - Fase di gioco (click sulla griglia nemica per sparare)
  - Schermata di fine partita con pulsante Esci
  - Chat testuale integrata
  - Disconnessione improvvisa senza crash
"""

import socket
import threading
import json
import sys
import pygame

# ─────────────────────────────────────────────
# CONFIGURAZIONE CONNESSIONE
# ─────────────────────────────────────────────
SERVER_HOST = "127.0.0.1"   # Cambia con l'IP del server se in rete locale
SERVER_PORT = 50016

# ─────────────────────────────────────────────
# DIMENSIONI  –  tutto deriva da CELL
# ─────────────────────────────────────────────
CELL      = 32          # px per cella: riduci se lo schermo è piccolo
GRID_SIZE = 10
GRID_PX   = CELL * GRID_SIZE   # 320 px

LABEL_H   = 20          # altezza riga etichette colonne (A-J)
COORD_W   = 20          # larghezza colonna etichette riga (1-10)

# Griglia completa di etichette
GFULL_W = COORD_W + GRID_PX    # 340
GFULL_H = LABEL_H + GRID_PX    # 340

# Margini e gap tra i due blocchi
TITLE_H      = 36       # altezza area titolo
M_LEFT       = 14       # margine sinistro
M_TOP        = TITLE_H + 8
GAP_GRIDS    = 22       # spazio orizzontale tra le due griglie
STATUS_H     = 44       # barra di stato sotto le griglie
M_BOTTOM     = 10

# Pannello destro
PANEL_W      = 220
PANEL_GAP    = 16       # gap tra griglie e pannello
INFO_H       = 180      # sezione info/stato nel pannello
BTN_H        = 36       # altezza pulsanti
BTN_GAP      = 8        # gap tra pulsanti e chat
CHAT_INPUT_H = 30

# ── Dimensioni finestra calcolate ───────────
WIN_W = M_LEFT + GFULL_W * 2 + GAP_GRIDS + PANEL_GAP + PANEL_W + M_LEFT
# altezza: determinata dal lato più alto (griglie vs pannello)
GRIDS_H = M_TOP + GFULL_H + STATUS_H + M_BOTTOM
PANEL_CONTENT_H = M_TOP + INFO_H + BTN_GAP + BTN_H + BTN_GAP + M_BOTTOM
WIN_H = max(GRIDS_H, 580)   # minimo 580 per la chat

# Posizione x del pannello destro
PANEL_X = M_LEFT + GFULL_W * 2 + GAP_GRIDS + PANEL_GAP
# Altezza disponibile per la chat (riempe lo spazio rimanente)
CHAT_H  = WIN_H - M_TOP - INFO_H - BTN_GAP - BTN_H - BTN_GAP - CHAT_INPUT_H - M_BOTTOM

# ─────────────────────────────────────────────
# PALETTE COLORI  (toni naturali, niente neon)
# ─────────────────────────────────────────────
C_BG         = (232, 229, 222)
C_PANEL      = (218, 214, 206)
C_GRID_EMPTY = (198, 213, 228)
C_GRID_LINE  = (155, 172, 188)
C_SHIP       = ( 95, 118, 142)
C_HIT        = (182,  62,  52)
C_MISS       = (125, 168, 198)
C_SUNK       = (135,  78,  28)
C_HOVER      = (168, 198, 218)
C_PREVIEW_OK = ( 98, 162, 108)
C_PREVIEW_NO = (188,  88,  78)
C_TEXT       = ( 42,  42,  42)
C_TEXT_DIM   = (118, 112, 105)
C_ACCENT     = ( 58,  88, 128)
C_TITLE      = ( 32,  52,  82)
C_CHAT_BG    = (208, 204, 196)
C_CHAT_IN    = (225, 221, 213)
C_BTN_OK     = ( 72, 118, 158)
C_BTN_DIS    = (168, 162, 155)
C_BTN_HOV    = ( 52,  92, 138)
C_WIN_BG     = (198, 228, 202)
C_LOSE_BG    = (228, 202, 198)
C_DISC_BG    = (222, 212, 192)

# ─────────────────────────────────────────────
# FLOTTA STANDARD
# ─────────────────────────────────────────────
FLEET_TEMPLATE = [
    ("Portaerei",          5),
    ("Corazzata",          4),
    ("Incrociatore",       3),
    ("Sottomarino",        3),
    ("Cacciatorpediniere", 2),
]

COLS = "ABCDEFGHIJ"


# ─────────────────────────────────────────────
# COMUNICAZIONE TCP
# ─────────────────────────────────────────────

def send_msg(sock, msg: dict):
    """Serializza e invia un messaggio JSON delimitato da newline."""
    try:
        sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
    except Exception as e:
        print(f"[CLIENT] Errore invio: {e}")


def recv_msg(sock) -> dict | None:
    """
    Legge dal socket fino al newline e deserializza il JSON.
    Restituisce None se la connessione è chiusa o c'è un errore.
    """
    buf = ""
    try:
        while True:
            chunk = sock.recv(4096).decode("utf-8")
            if not chunk:
                return None
            buf += chunk
            if "\n" in buf:
                line, _ = buf.split("\n", 1)
                return json.loads(line)
    except Exception:
        return None


# ─────────────────────────────────────────────
# STATO DI GIOCO
# ─────────────────────────────────────────────

class GameState:
    """
    Contenitore di tutto lo stato condiviso tra il thread grafico
    e il thread di rete. Il threading.Lock protegge gli accessi concorrenti.

    Fasi:
      "connecting"   → attesa del welcome dal server
      "placing"      → posizionamento navi
      "placing_done" → griglia inviata, attesa avversario
      "my_turn"      → spariamo noi
      "waiting"      → turno avversario
      "game_over"    → partita finita
    """
    def __init__(self):
        self.lock             = threading.Lock()
        self.phase            = "connecting"
        self.player_id        = None
        self.my_grid          = [[0]*GRID_SIZE for _ in range(GRID_SIZE)]
        self.my_hits          = {}   # {(r,c): "ACQUA"|"COLPITO"|"AFFONDATO"}
        self.enemy_hits       = {}
        self.fleet_list       = list(FLEET_TEMPLATE)
        self.current_ship_idx = 0
        self.ship_horizontal  = True
        self.chat_messages    = []
        self.chat_input       = ""
        self.status_msg          = "Connessione al server..."
        self.winner              = None
        self.disconnect_msg      = ""
        self.game_over_received  = False   # True solo dopo un game_over regolare

    def add_chat(self, who: str, text: str):
        """Aggiunge una riga alla chat (max 80 righe)."""
        with self.lock:
            self.chat_messages.append(f"{who}: {text}")
            if len(self.chat_messages) > 80:
                self.chat_messages.pop(0)


# ─────────────────────────────────────────────
# THREAD DI RETE
# ─────────────────────────────────────────────

def network_thread(sock, state: GameState):
    """
    Ascolta in loop i messaggi dal server e aggiorna GameState.
    Ogni tipo di messaggio corrisponde a una transizione di fase.
    Gira su un thread daemon separato dal loop grafico.
    """
    while True:
        msg = recv_msg(sock)

        if msg is None:
            with state.lock:
                # Il server chiude il socket dopo game_over E dopo disconnect.
                # Usiamo game_over_received (impostato atomicamente col winner)
                # per distinguere i due casi senza race condition.
                if not state.game_over_received:
                    state.disconnect_msg = "Connessione al server persa."
                state.phase = "game_over"
            return

        t = msg.get("type")

        if t == "welcome":
            with state.lock:
                state.player_id  = msg["player_id"]
                state.status_msg = (
                    f"Sei il Giocatore {state.player_id + 1}. "
                    "Posiziona le navi (click + R per ruotare)."
                )
                state.phase = "placing"

        elif t == "start":
            with state.lock:
                if msg.get("your_turn"):
                    state.phase      = "my_turn"
                    state.status_msg = "Partita iniziata — Tocca a te! Clicca sulla griglia nemica."
                else:
                    state.phase      = "waiting"
                    state.status_msg = "Partita iniziata — Aspetta il turno avversario."

        elif t == "shot_result":
            r, c     = msg["row"], msg["col"]
            result   = msg["result"]
            attacker = msg["attacker"]
            coord    = f"{COLS[c]}{r+1}"
            with state.lock:
                pid = state.player_id
                if attacker == pid:
                    state.enemy_hits[(r, c)] = result
                    if result == "ACQUA":
                        state.status_msg = f"Acqua in {coord}. Turno avversario."
                        state.phase      = "waiting"
                    elif result == "COLPITO":
                        state.status_msg = f"Colpito in {coord}! Spara ancora."
                        state.phase      = "my_turn"
                    else:
                        state.status_msg = f"Affondata in {coord}! Spara ancora."
                        state.phase      = "my_turn"
                else:
                    state.my_hits[(r, c)] = result
                    if result == "ACQUA":
                        state.status_msg = "L'avversario ha mancato. Tocca a te!"
                        state.phase      = "my_turn"
                    elif result == "COLPITO":
                        state.status_msg = f"Colpiti in {coord}. Attendi..."
                        state.phase      = "waiting"
                    else:
                        state.status_msg = f"Nave affondata in {coord}. Attendi..."
                        state.phase      = "waiting"

        elif t == "game_over":
            with state.lock:
                state.winner             = msg["winner"]
                state.game_over_received = True          # flag anti-race condition
                state.phase              = "game_over"
                state.status_msg = (
                    "Hai vinto!" if state.winner == state.player_id else "Hai perso."
                )

        elif t == "disconnect":
            with state.lock:
                state.winner         = msg.get("winner", state.player_id)
                state.disconnect_msg = msg.get("msg", "Avversario disconnesso.")
                state.phase          = "game_over"
            return

        elif t == "chat":
            state.add_chat(f"Giocatore {msg['from'] + 1}", msg.get("text", ""))


# ─────────────────────────────────────────────
# LOGICA POSIZIONAMENTO NAVI
# ─────────────────────────────────────────────

def can_place(grid, r, c, length, horizontal) -> bool:
    """
    Controlla se la nave (lunga 'length') può essere piazzata
    a partire da (r,c) senza uscire dalla griglia e senza sovrapposizioni.
    """
    for i in range(length):
        nr = r + (0 if horizontal else i)
        nc = c + (i if horizontal else 0)
        if nr >= GRID_SIZE or nc >= GRID_SIZE:
            return False
        if grid[nr][nc] != 0:
            return False
    return True


def place_ship(grid, r, c, length, horizontal, ship_id: int):
    """Scrive ship_id nelle celle occupate dalla nave."""
    for i in range(length):
        nr = r + (0 if horizontal else i)
        nc = c + (i if horizontal else 0)
        grid[nr][nc] = ship_id


# ─────────────────────────────────────────────
# HELPER GRAFICI
# ─────────────────────────────────────────────

def grid_origin(grid_index: int) -> tuple:
    """
    Restituisce (ox, oy): angolo in alto a sinistra delle CELLE
    (escluse le etichette coordinate) per la griglia 0 o 1.
    """
    ox = M_LEFT + grid_index * (GFULL_W + GAP_GRIDS) + COORD_W
    oy = M_TOP + LABEL_H
    return ox, oy


def cell_from_mouse(grid_index: int, mx: int, my: int):
    """
    Converte le coordinate del mouse in (riga, colonna) della griglia.
    Restituisce None se il mouse è fuori dalla griglia.
    """
    ox, oy = grid_origin(grid_index)
    if mx < ox or my < oy:
        return None
    c = (mx - ox) // CELL
    r = (my - oy) // CELL
    if 0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE:
        return r, c
    return None


def draw_grid(surface, font_coord, grid_index: int, grid, hits: dict,
              show_ships: bool, hover=None, preview=None, preview_ok=True):
    """
    Disegna una griglia 10x10 con etichette A-J e 1-10,
    colorando le celle in base a: nave, colpo, hover, anteprima.
    """
    ox, oy = grid_origin(grid_index)
    label_y = M_TOP + 2

    # Etichette colonne A-J
    for c in range(GRID_SIZE):
        s = font_coord.render(COLS[c], True, C_TEXT_DIM)
        surface.blit(s, (ox + c*CELL + CELL//2 - s.get_width()//2, label_y))

    # Etichette righe 1-10
    for r in range(GRID_SIZE):
        s = font_coord.render(str(r+1), True, C_TEXT_DIM)
        surface.blit(s, (ox - COORD_W + 2, oy + r*CELL + CELL//2 - s.get_height()//2))

    prev_set = set(preview) if preview else set()

    for r in range(GRID_SIZE):
        for c in range(GRID_SIZE):
            x    = ox + c * CELL
            y    = oy + r * CELL
            rect = pygame.Rect(x, y, CELL, CELL)

            col = C_GRID_EMPTY
            if show_ships and grid[r][c] != 0:
                col = C_SHIP
            if (r, c) in hits:
                col = {"ACQUA": C_MISS, "COLPITO": C_HIT,
                       "AFFONDATO": C_SUNK}.get(hits[(r, c)], col)
            if (r, c) in prev_set:
                col = C_PREVIEW_OK if preview_ok else C_PREVIEW_NO
            if hover == (r, c) and (r, c) not in hits:
                col = C_HOVER

            pygame.draw.rect(surface, col, rect)
            pygame.draw.rect(surface, C_GRID_LINE, rect, 1)

    # Bordo esterno evidenziato
    pygame.draw.rect(surface, C_ACCENT, pygame.Rect(ox, oy, GRID_PX, GRID_PX), 2)


def draw_button(surface, font, text: str, rect: pygame.Rect, enabled=True) -> bool:
    """
    Disegna un pulsante. Restituisce True se il mouse è sopra (hover).
    """
    mx, my = pygame.mouse.get_pos()
    hover  = rect.collidepoint(mx, my) and enabled
    col    = C_BTN_HOV if hover else (C_BTN_OK if enabled else C_BTN_DIS)
    pygame.draw.rect(surface, col, rect, border_radius=5)
    lbl = font.render(text, True, (255, 255, 255) if enabled else (195, 190, 182))
    surface.blit(lbl, (rect.x + (rect.w - lbl.get_width())//2,
                        rect.y + (rect.h - lbl.get_height())//2))
    return hover


def draw_box(surface, x, y, w, h, color=None, border=True):
    """Rettangolo con sfondo opzionale e bordo sottile."""
    pygame.draw.rect(surface, color or C_PANEL, pygame.Rect(x, y, w, h), border_radius=4)
    if border:
        pygame.draw.rect(surface, C_GRID_LINE, pygame.Rect(x, y, w, h), 1, border_radius=4)


def wrap_text(text: str, font, max_w: int) -> list:
    """Spezza una stringa in righe che non superano max_w pixel."""
    words, lines, line = text.split(), [], ""
    for w in words:
        test = line + (" " if line else "") + w
        if font.size(test)[0] <= max_w:
            line = test
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


# ─────────────────────────────────────────────
# SCHERMATA FINE PARTITA
# ─────────────────────────────────────────────

def draw_game_over(surface, state: GameState, font_big, font, font_small, btn_exit):
    """
    Overlay di fine partita: sfondo semi-trasparente colorato + box centrale
    con risultato (vittoria / sconfitta / disconnessione) e pulsante Chiudi.
    """
    with state.lock:
        winner   = state.winner
        pid      = state.player_id
        disc_msg = state.disconnect_msg

    won  = (winner == pid)
    disc = bool(disc_msg)

    bg = C_DISC_BG if disc else (C_WIN_BG if won else C_LOSE_BG)
    overlay = pygame.Surface((WIN_W, WIN_H), pygame.SRCALPHA)
    overlay.fill((*bg, 210))
    surface.blit(overlay, (0, 0))

    bw, bh = 400, 185
    bx = WIN_W // 2 - bw // 2
    by = WIN_H // 2 - bh // 2
    pygame.draw.rect(surface, (250, 248, 244), pygame.Rect(bx, by, bw, bh), border_radius=8)
    pygame.draw.rect(surface, C_ACCENT,        pygame.Rect(bx, by, bw, bh), 2, border_radius=8)

    if disc:
        title, tcol = "Avversario disconnesso", (135, 98, 38)
        sub = disc_msg
    elif won:
        title, tcol = "Hai vinto!", (48, 108, 58)
        sub = "Complimenti! Hai affondato tutta la flotta nemica."
    else:
        title, tcol = "Hai perso.", (148, 48, 38)
        sub = "L'avversario ha affondato tutta la tua flotta."

    ts = font_big.render(title, True, tcol)
    surface.blit(ts, (WIN_W//2 - ts.get_width()//2, by + 16))

    for i, line in enumerate(wrap_text(sub, font_small, bw - 30)[:3]):
        ls = font_small.render(line, True, C_TEXT)
        surface.blit(ls, (WIN_W//2 - ls.get_width()//2, by + 60 + i*19))

    # Pulsante Chiudi centrato nel box
    btn_exit.x = WIN_W // 2 - btn_exit.w // 2
    btn_exit.y = by + bh - BTN_H - 14
    draw_button(surface, font, "Chiudi", btn_exit, enabled=True)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    """
    Entry point: inizializza Pygame, connette il socket,
    avvia il thread di rete e gestisce il loop grafico a 60 fps.
    """
    pygame.init()
    screen = pygame.display.set_mode((WIN_W, WIN_H))
    pygame.display.set_caption("Battaglia Navale")
    clock = pygame.time.Clock()

    font       = pygame.font.SysFont("monospace", 13, bold=True)
    font_big   = pygame.font.SysFont("monospace", 22, bold=True)
    font_small = pygame.font.SysFont("monospace", 12)
    font_coord = pygame.font.SysFont("monospace", 11)
    font_title = pygame.font.SysFont("monospace", 17, bold=True)

    # ── Connessione TCP ──────────────────────
    state = GameState()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((SERVER_HOST, SERVER_PORT))
    except Exception as e:
        screen.fill(C_BG)
        screen.blit(font.render(f"Connessione fallita: {e}", True, C_HIT), (20, WIN_H//2 - 20))
        screen.blit(font_small.render("Assicurati che il server sia avviato.", True, C_TEXT_DIM),
                    (20, WIN_H//2 + 4))
        pygame.display.flip()
        pygame.time.wait(4000)
        pygame.quit()
        sys.exit(1)

    threading.Thread(target=network_thread, args=(sock, state), daemon=True).start()

    # ── Posizioni fisse pannello destro ─────
    px = PANEL_X
    py = M_TOP

    # Info box (parte alta pannello)
    info_y = py
    # Pulsanti sotto info box
    btn_y      = info_y + INFO_H + BTN_GAP
    btn_rotate = pygame.Rect(px,                   btn_y, PANEL_W//2 - 4, BTN_H)
    btn_ready  = pygame.Rect(px + PANEL_W//2 + 4,  btn_y, PANEL_W//2 - 4, BTN_H)
    # Chat sotto i pulsanti
    chat_y     = btn_y + BTN_H + BTN_GAP
    input_y    = chat_y + CHAT_H

    # Pulsante uscita fine partita
    btn_exit = pygame.Rect(0, 0, 150, BTN_H)

    # Barra di stato: sotto le griglie
    status_y = M_TOP + GFULL_H + 8

    running     = True
    hover_enemy = None

    while running:
        clock.tick(60)
        screen.fill(C_BG)

        # ── Snapshot thread-safe ─────────────
        with state.lock:
            phase      = state.phase
            my_grid    = [row[:] for row in state.my_grid]
            my_hits    = dict(state.my_hits)
            enemy_hits = dict(state.enemy_hits)
            ship_idx   = state.current_ship_idx
            horizontal = state.ship_horizontal
            status     = state.status_msg
            winner     = state.winner
            pid        = state.player_id
            fleet_list = state.fleet_list

        mx, my = pygame.mouse.get_pos()

        # ── Anteprima posizionamento ─────────
        preview, preview_ok = [], False
        if phase == "placing" and ship_idx < len(fleet_list):
            cell = cell_from_mouse(0, mx, my)
            if cell:
                r, c = cell
                _, length = fleet_list[ship_idx]
                preview_ok = can_place(my_grid, r, c, length, horizontal)
                for i in range(length):
                    nr = r + (0 if horizontal else i)
                    nc = c + (i if horizontal else 0)
                    if 0 <= nr < GRID_SIZE and 0 <= nc < GRID_SIZE:
                        preview.append((nr, nc))

        # ── Hover griglia nemica ─────────────
        hover_enemy = None
        if phase == "my_turn":
            hc = cell_from_mouse(1, mx, my)
            if hc and hc not in enemy_hits:
                hover_enemy = hc

        # ── TITOLO ──────────────────────────
        t_surf = font_title.render("BATTAGLIA NAVALE", True, C_TITLE)
        screen.blit(t_surf, (WIN_W//2 - t_surf.get_width()//2, 8))

        # ── GRIGLIE ─────────────────────────
        draw_grid(screen, font_coord, 0, my_grid, my_hits,
                  show_ships=True, preview=preview, preview_ok=preview_ok)
        draw_grid(screen, font_coord, 1, [[0]*GRID_SIZE]*GRID_SIZE, enemy_hits,
                  show_ships=False, hover=hover_enemy)

        # Etichette sopra le griglie
        ox0, _ = grid_origin(0)
        ox1, _ = grid_origin(1)
        label0 = font.render(f"TUA GRIGLIA  (G{(pid or 0)+1})", True, C_ACCENT)
        label1 = font.render("GRIGLIA NEMICA", True, C_ACCENT)
        screen.blit(label0, (ox0 - COORD_W, M_TOP - 18))
        screen.blit(label1, (ox1 - COORD_W, M_TOP - 18))

        # ── BARRA DI STATO ───────────────────
        bar_col = (196, 220, 198) if phase == "my_turn" else C_BG
        bar_rect = pygame.Rect(M_LEFT, status_y, GFULL_W*2 + GAP_GRIDS, STATUS_H - 6)
        pygame.draw.rect(screen, bar_col, bar_rect, border_radius=4)

        st_surf = font.render(status[:70], True, C_TEXT)
        screen.blit(st_surf, (M_LEFT + 6, status_y + 4))

        if phase == "my_turn":
            tl = font_small.render("▶ IL TUO TURNO — clicca sulla griglia nemica per sparare", True, (52, 112, 62))
            screen.blit(tl, (M_LEFT + 6, status_y + 22))
        elif phase == "waiting":
            tl = font_small.render("⏳ Turno avversario — aspetta...", True, C_TEXT_DIM)
            screen.blit(tl, (M_LEFT + 6, status_y + 22))
        elif phase == "placing":
            remaining = len(fleet_list) - ship_idx
            tl = font_small.render(
                f"Navi piazzate: {ship_idx}/{len(fleet_list)}  |  R = ruota orientamento",
                True, C_TEXT_DIM)
            screen.blit(tl, (M_LEFT + 6, status_y + 22))

        # ── PANNELLO DESTRO: INFO ────────────
        draw_box(screen, px, info_y, PANEL_W, INFO_H)

        if phase in ("placing", "placing_done"):
            # Mostra info sulla nave da piazzare
            draw_box(screen, px, info_y, PANEL_W, INFO_H)
            header = font.render("POSIZIONAMENTO", True, C_ACCENT)
            screen.blit(header, (px + 6, info_y + 6))

            if phase == "placing" and ship_idx < len(fleet_list):
                name, length = fleet_list[ship_idx]
                lines = [
                    f"Nave corrente:",
                    f"  {name}  ({length} celle)",
                    f"",
                    f"Orientamento:",
                    f"  {'→ Orizzontale' if horizontal else '↓ Verticale'}",
                    f"",
                    f"Navi piazzate: {ship_idx}/{len(fleet_list)}",
                    f"",
                    f"R = cambia orientamento",
                    f"Click = piazza sulla griglia",
                ]
            elif phase == "placing_done":
                lines = [
                    "Griglia inviata.",
                    "",
                    "Attendo che anche",
                    "l'avversario finisca",
                    "il posizionamento...",
                ]
            else:
                lines = ["Tutte le navi piazzate!", "", "Premi 'Pronto!' per", "iniziare la partita."]

            for i, line in enumerate(lines):
                s = font_small.render(line, True, C_TEXT)
                screen.blit(s, (px + 6, info_y + 24 + i * 15))

        elif phase in ("my_turn", "waiting"):
            # Legenda colori
            draw_box(screen, px, info_y, PANEL_W, INFO_H)
            header = font.render("LEGENDA", True, C_ACCENT)
            screen.blit(header, (px + 6, info_y + 6))

            legend = [
                ("Tua nave",    C_SHIP),
                ("Tua colpita", C_HIT),
                ("Tua affondata", C_SUNK),
                ("Tua mancata",  C_MISS),
                (None, None),
                ("Nemica colpita",   C_HIT),
                ("Nemica affondata", C_SUNK),
                ("Nemica acqua",     C_MISS),
            ]
            for i, (label, color) in enumerate(legend):
                if label is None:
                    continue
                y_i = info_y + 26 + i * 17
                if color:
                    pygame.draw.rect(screen, color, pygame.Rect(px+6, y_i+1, 12, 12), border_radius=2)
                s = font_small.render(label, True, C_TEXT)
                screen.blit(s, (px + 22, y_i))

        elif phase == "connecting":
            s = font_small.render("Connessione in corso...", True, C_TEXT_DIM)
            screen.blit(s, (px + 6, info_y + 20))

        # ── PULSANTI (solo in fase placing) ──
        if phase == "placing":
            draw_button(screen, font, "Ruota (R)", btn_rotate, enabled=True)
            all_placed = ship_idx >= len(fleet_list)
            draw_button(screen, font, "Pronto!", btn_ready, enabled=all_placed)
        elif phase == "placing_done":
            # Mostra disabilitati per feedback visivo
            draw_button(screen, font, "Ruota (R)", btn_rotate, enabled=False)
            draw_button(screen, font, "Pronto!", btn_ready, enabled=False)

        # ── CHAT ────────────────────────────
        draw_box(screen, px, chat_y, PANEL_W, CHAT_H, color=C_CHAT_BG)
        chat_lbl = font_small.render("Chat  (Invio = invia)", True, C_TEXT_DIM)
        screen.blit(chat_lbl, (px + 4, chat_y - 16))

        lh = font_small.get_height() + 2
        max_rows = (CHAT_H - 4) // lh
        with state.lock:
            msgs = state.chat_messages[-max_rows:]
        for i, m in enumerate(msgs):
            screen.blit(font_small.render(m[:34], True, C_TEXT), (px+4, chat_y+3+i*lh))

        # Input chat
        draw_box(screen, px, input_y, PANEL_W, CHAT_INPUT_H, color=C_CHAT_IN)
        pygame.draw.rect(screen, C_ACCENT, pygame.Rect(px, input_y, PANEL_W, CHAT_INPUT_H), 1, border_radius=4)
        with state.lock:
            inp = state.chat_input
        screen.blit(font_small.render(inp[-36:] + "|", True, C_TEXT),
                    (px+5, input_y + (CHAT_INPUT_H - font_small.get_height())//2))

        # ── GAME OVER OVERLAY ────────────────
        if phase == "game_over":
            draw_game_over(screen, state, font_big, font, font_small, btn_exit)

        # ── EVENTI ──────────────────────────
        for event in pygame.event.get():

            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.KEYDOWN:

                if event.key == pygame.K_r and phase == "placing":
                    with state.lock:
                        state.ship_horizontal = not state.ship_horizontal

                elif event.key == pygame.K_RETURN:
                    with state.lock:
                        text             = state.chat_input.strip()
                        state.chat_input = ""
                    if text and phase not in ("connecting",):
                        send_msg(sock, {"type": "chat", "text": text})
                        state.add_chat(f"Tu (G{(pid or 0)+1})", text)

                elif event.key == pygame.K_BACKSPACE:
                    with state.lock:
                        state.chat_input = state.chat_input[:-1]

                else:
                    ch = event.unicode
                    if ch and ch.isprintable():
                        with state.lock:
                            if len(state.chat_input) < 60:
                                state.chat_input += ch

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx2, my2 = event.pos

                if phase == "game_over" and btn_exit.collidepoint(mx2, my2):
                    running = False

                elif phase == "placing" and btn_rotate.collidepoint(mx2, my2):
                    with state.lock:
                        state.ship_horizontal = not state.ship_horizontal

                elif phase == "placing" and btn_ready.collidepoint(mx2, my2):
                    with state.lock:
                        all_placed = state.current_ship_idx >= len(state.fleet_list)
                    if all_placed:
                        with state.lock:
                            g                  = [row[:] for row in state.my_grid]
                            state.phase        = "placing_done"
                            state.status_msg   = "Griglia inviata. Attendo l'avversario..."
                        send_msg(sock, {"type": "placement", "grid": g})

                elif phase == "placing":
                    cell = cell_from_mouse(0, mx2, my2)
                    if cell:
                        with state.lock:
                            sidx = state.current_ship_idx
                            if sidx < len(state.fleet_list):
                                name, length = state.fleet_list[sidx]
                                r, c         = cell
                                horiz        = state.ship_horizontal
                                if can_place(state.my_grid, r, c, length, horiz):
                                    place_ship(state.my_grid, r, c, length, horiz, sidx+1)
                                    state.current_ship_idx += 1
                                    if state.current_ship_idx >= len(state.fleet_list):
                                        state.status_msg = "Tutte le navi piazzate! Premi 'Pronto!' per iniziare."

                elif phase == "my_turn":
                    cell = cell_from_mouse(1, mx2, my2)
                    if cell and cell not in enemy_hits:
                        r, c = cell
                        send_msg(sock, {"type": "shot", "row": r, "col": c})
                        with state.lock:
                            state.phase      = "waiting"
                            state.status_msg = f"Colpo sparato in {COLS[c]}{r+1}..."

        pygame.display.flip()

    try:
        sock.close()
    except Exception:
        pass
    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()