import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE, WS_BASE } from "./api";

/**
 * Owns the game WebSocket for a (code, token) pair.
 *
 * The server pushes a full `state` snapshot on every connect and after every
 * mutation, so this hook keeps only the latest snapshot and the app renders
 * from it — no client-side game logic. Reconnects with capped exponential
 * backoff; recovery is automatic because reconnect => fresh snapshot.
 *
 * Lifecycle rule (the fix for "clicks do nothing"): at most ONE live socket,
 * and it is always `wsRef.current`. Every handler checks that the socket it
 * belongs to is still the current one; events from stale sockets (closed by
 * cleanup, replaced after a drop, StrictMode's dev double-mount) are ignored
 * completely — no state updates, no reconnect timers, no auth-failure counts.
 * Without this, a stale socket's late `onclose` could schedule a duplicate
 * connection that hijacks wsRef while the old one kept feeding snapshots:
 * the UI looked live but every send() went into a dead socket.
 *
 * Returns {game, connected, authFailed, removed, lastError, lastBuzz,
 *          revealedAnswer, clearError, send}.
 */
export function useGameSocket(code, token) {
  const [game, setGame] = useState(null);
  const [connected, setConnected] = useState(false);
  const [authFailed, setAuthFailed] = useState(false);
  // §F (Handoff #11): the host kicked this seat. Set by the documented
  // structured error {code: "player_removed"} or the app close code 4003.
  // (A kicked seat's RECONNECT attempts land on the 4001/authFailed path
  // instead — the server filters removed tokens at connect.)
  const [removed, setRemoved] = useState(false);
  const [lastError, setLastError] = useState(null);
  const [lastBuzz, setLastBuzz] = useState(null); // {participant_id, name, order, at}
  const [revealedAnswer, setRevealedAnswer] = useState(null);

  const wsRef = useRef(null);
  const retryRef = useRef(0);
  const timerRef = useRef(null);
  const closedByUs = useRef(false);
  const rejectedRef = useRef(0); // consecutive closes where the socket never opened
  const prevBuzzesRef = useRef(0); // buzz count of the last snapshot's open cell

  const openCellId = game?.current_cell?.id ?? null;
  const openCellRef = useRef(openCellId);
  openCellRef.current = openCellId;

  useEffect(() => {
    if (!code || !token) return undefined;
    closedByUs.current = false;
    rejectedRef.current = 0;

    const connect = () => {
      // Only one socket may exist. Cancel any pending reconnect and bail if
      // a current socket is already open or connecting.
      clearTimeout(timerRef.current);
      const existing = wsRef.current;
      if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) {
        return;
      }

      const ws = new WebSocket(`${WS_BASE}/ws/game/${code.toUpperCase()}/?token=${encodeURIComponent(token)}`);
      wsRef.current = ws;
      const isCurrent = () => wsRef.current === ws && !closedByUs.current;

      let opened = false;
      ws.onopen = () => {
        if (!isCurrent()) {
          ws.close();
          return;
        }
        opened = true;
        retryRef.current = 0;
        rejectedRef.current = 0;
        setConnected(true);
      };

      ws.onmessage = (evt) => {
        if (!isCurrent()) return;
        let msg;
        try {
          msg = JSON.parse(evt.data);
        } catch {
          return;
        }
        if (msg.type === "state") {
          const next = msg.game;
          // Clear per-question transient UI when the open cell changes.
          if ((next?.current_cell?.id ?? null) !== openCellRef.current) {
            setRevealedAnswer(null);
            setLastBuzz(null);
          } else {
            // The backend doesn't emit the incremental `buzz` event — every
            // buzz arrives as a new snapshot. Synthesize lastBuzz from the
            // diff so haptics/sound still fire the instant the list grows.
            const prev = prevBuzzesRef.current;
            const buzzes = next?.current_cell?.buzzes ?? [];
            if (buzzes.length > prev) {
              const b = buzzes[buzzes.length - 1];
              setLastBuzz({ ...b, order: buzzes.length, at: Date.now() });
            }
          }
          prevBuzzesRef.current = next?.current_cell?.buzzes?.length ?? 0;
          setGame(next);
        } else if (msg.type === "buzz") {
          setLastBuzz({ ...msg, at: Date.now() });
        } else if (msg.type === "answer_reveal") {
          setRevealedAnswer(msg.answer);
        } else if (msg.type === "error") {
          if (msg.code === "player_removed") setRemoved(true); // §F (#11)
          setLastError(msg.detail || "Action failed.");
        }
      };

      ws.onclose = async (evt) => {
        // A stale socket closing is not news; only the current one matters.
        if (wsRef.current !== ws) return;
        wsRef.current = null;
        setConnected(false);
        if (closedByUs.current) return;
        if (evt.code === 4001) {
          setAuthFailed(true); // bad/expired participant token — caller clears seat
          return;
        }
        if (evt.code === 4003) {
          setRemoved(true); // §F (#11): the host removed this seat — no retry
          return;
        }

        // An invalid token never reaches the browser as 4001: the server
        // closes before accepting the handshake, so daphne answers HTTP 403
        // and the browser sees a generic close (1006) with `opened` false.
        // Two rejected handshakes in a row + a working REST API for this game
        // means our token is bad (server-down would fail the probe too).
        if (!opened) {
          rejectedRef.current += 1;
          if (rejectedRef.current >= 2) {
            try {
              const res = await fetch(`${API_BASE}/api/games/${code.toUpperCase()}/`);
              if (closedByUs.current || wsRef.current) return; // unmounted or a newer socket took over
              if (res.ok || res.status === 404) {
                setAuthFailed(true); // server is fine; the seat token is not (or game is gone)
                return;
              }
            } catch {
              // server unreachable — fall through to backoff and keep trying
            }
            if (closedByUs.current || wsRef.current) return;
          }
        }

        const delay = Math.min(500 * 2 ** retryRef.current, 8000);
        retryRef.current += 1;
        clearTimeout(timerRef.current);
        timerRef.current = setTimeout(connect, delay);
      };

      ws.onerror = () => {
        if (wsRef.current === ws) ws.close();
      };
    };

    connect();
    return () => {
      closedByUs.current = true;
      clearTimeout(timerRef.current);
      const ws = wsRef.current;
      wsRef.current = null;
      if (ws) {
        // Detach before closing so late events from this socket are inert.
        ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
        ws.close();
      }
    };
  }, [code, token]);

  const send = useCallback((action, payload = {}) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setLastError("Not connected — hang on, reconnecting…");
      return;
    }
    ws.send(JSON.stringify({ action, ...payload }));
  }, []);

  const clearError = useCallback(() => setLastError(null), []);

  return { game, connected, authFailed, removed, lastError, lastBuzz, revealedAnswer, clearError, send };
}
