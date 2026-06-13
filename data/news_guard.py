"""
data/news_guard.py
Bloquea nuevas entradas alrededor de eventos de alto impacto (ForexFactory).
Cachea resultados 15 minutos para no saturar la API.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request

log = logging.getLogger(__name__)

_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


class NewsGuard:
    """
    Uso:
        guard = NewsGuard(cfg)
        if guard.is_blocked():
            continue  # no abrir trades
    """

    def __init__(self, cfg: dict):
        r = cfg.get("news", {})
        self.enabled     = r.get("enabled", True)
        self.pre_min     = r.get("pre_news_minutes",  10)
        self.post_min    = r.get("post_news_minutes", 15)
        self.impact      = r.get("impact_filter", "High")

        self._events: list  = []
        self._fetched_at    = 0.0
        self._cache_ttl     = 900  # 15 minutos
        self._lock          = threading.Lock()

    # ------------------------------------------------------------------ #

    def is_blocked(self) -> bool:
        """True si el momento actual está dentro de la ventana pre/post noticia."""
        if not self.enabled:
            return False
        self._refresh()
        now = datetime.now(timezone.utc)
        for ev in self._events:
            dt_utc = ev.get("dt_utc")
            if dt_utc is None:
                continue
            diff_min = (now - dt_utc).total_seconds() / 60
            # diff_min > 0 → evento ya pasó; diff_min < 0 → evento futuro
            if -self.pre_min <= diff_min <= self.post_min:
                log.info("NewsGuard BLOQUEADO: %s (%.0f min)", ev.get("title", "?"), diff_min)
                return True
        return False

    # ------------------------------------------------------------------ #

    def _refresh(self):
        with self._lock:
            if time.time() - self._fetched_at < self._cache_ttl:
                return
            try:
                req = Request(_FF_URL, headers={"User-Agent": "Mozilla/5.0"})
                raw = json.loads(urlopen(req, timeout=8).read().decode())
                events = []
                for e in raw:
                    if e.get("country") != "USD":
                        continue
                    if self.impact == "High" and e.get("impact") != "High":
                        continue
                    if self.impact == "Medium" and e.get("impact") not in ("High", "Medium"):
                        continue
                    try:
                        ds     = f"{e.get('date','')} {e.get('time','')}"
                        dt_et  = datetime.strptime(ds, "%m/%d/%Y %I:%M%p")
                        # ForexFactory usa ET (EDT ≈ UTC-4 en verano, EST ≈ UTC-5 en invierno)
                        # Usamos UTC-4 como aproximación (ajustable si necesario)
                        dt_utc = dt_et.replace(tzinfo=timezone(timedelta(hours=-4)))
                        events.append({"title": e.get("title", ""), "dt_utc": dt_utc})
                    except Exception:
                        pass
                self._events     = events
                self._fetched_at = time.time()
                log.debug("NewsGuard: %d eventos USD cargados", len(events))
            except Exception as exc:
                log.warning("NewsGuard: no se pudo cargar calendario (%s) — no bloqueando", exc)
                self._fetched_at = time.time()  # evitar loops de reintentos
