# HLS SerienScraper - Upgrade Plan (2026)

## 📊 Übersicht
Dieses Dokument enthält einen umfassenden Upgrade-Plan für alle identifizierten Schwachstellen.

---

## 🔴 KRITISCHE PRIORITÄT - Sofort nötig

### 1. Browser-Pool Memory Management
**Problem**: Bei langen Downloads (> mehrere Stunden) werden Browser-Contexts nicht korrekt recycled, was zu Memory Leaks führt.

**Lösung**:
- Implementiere Hard-Limit auf Max-Context-Usecount (aktuell: 75, sollte reduziert werden)
- Automatischer Pool-Reset bei >20% Memory Usage
- Context-Leak-Detection mit GC-Tracing

**Dateien**: `app/browser_pool.py`

---

### 2. Popup-Behandlung für 18+ Seiten
**Problem**: Popups werden nur als Text-Filter erkannt, nicht als iframe/modal - führt zu "Page blocked" Fehlern.

**Lösung**:
- iframe-detection: Suche nach `<iframe>` und modal-Elementen
- Scroll-based detection: Wenn Seite scrollt um Popup-Inhalte zu laden
- Timeout-basierte Wartezeit für verzögerte Popups
- Fallback: Manuelle Bestätigung durch UI-Befehl

**Dateien**: `app/hls_downloader_final.py`

---

### 3. Parallelismus-Limit in der Queue
**Problem**: `max_parallel_limit=10` ist hart eingestellt, aber `max_parallel_downloads=3` ignoriert das Limit bei langen Downloads.

**Lösung**:
- Dynamisches Limit basierend auf verfügbaren Browser-Contexts
- Automatische Reduzierung bei hohem Memory-Pressure
- Queue-basierte Throttling-Implementierung

**Dateien**: `app/download_queue.py`, `app/web_gui.py`

---

## 🟠 HOHE PRIORITÄT - Bald implementieren

### 4. Batch-Download Parallelisierung
**Problem**: Serien werden nacheinander heruntergeladen, nicht parallel.

**Lösung**:
- Hintergrund-Skraper für mehrere URLs gleichzeitig
- Thread-Pool für Download-Erfassungen (nicht Browser-Pool!)
- Async Queue mit `asyncio.Semaphore` für Rate-Limiting

**Dateien**: `app/series_catalog.py`, `app/web_gui.py`

---

### 5. Drag-and-Drop Queue Reordering
**Problem**: Frontend hat Drag-and-Drop UI, aber Backend unterstützt keine Reihenfolge-Speicherung.

**Lösung**:
- Speichere Queue-Reihenfolge im JSON-Footerfeld
- API-Erweiterung für `reorder_queue()` mit Session-IDs Array
- Persistente Prioritätsliste in der Queue-Konfiguration

**Dateien**: `app/download_queue.py`, `app/routes/settings_routes.py`

---

### 6. Background Scraper optimieren
**Problem**: Auto-Scraper skrapet alle 25s, aber skript blockiert bei jeder Scraping-Anforderung.

**Lösung**:
- Hintergrund-Prozess (Subprocess/Thread-Pool) für Skraping
- Separate Browser-Instance nur für auto-update
- Batch-Updates: mehrere Serien gleichzeitig scraped, nicht eine nach der anderen

**Dateien**: `app/web_gui.py`

---

## 🟢 MITTEL PRIORITÄT - Geplante Verbesserungen

### 7. Ad-Blocking mit Updates
**Problem**: Filter-Cache veraltet, keine automatische Update-Mechanismen.

**Lösung**:
- Edge-Filterlisten integration (adsbc.net, etc.)
- Automatische Check alle 24 Stunden auf Updates
- In-Memory Filter-Caching mit TTL

---

### 8. Cover-Bild Caching Verbessern
**Problem**: Bilder werden mehrmals geladen bei Katalog-Browsing.

**Lösung**:
- Bild-Komprimierung vor dem Speichern (PIL)
- WebP-Format-Conversion für schnelleres Laden
- CDN-freundliches Resizing

---

### 9. Verbesserte Fallback Logik
**Problem**: Bei Browser-Ausfall ist nur Hard-Reboot möglich.

**Lösung**:
- Graceful Degradation: Single-Browser-Fallback
- Queue-Persistence mit Auto-Save alle 5 Minuten
- Download-Fortschrift bei Neustart automatisch wieder aufnehmen

---

## 🟡 NIEDRIGE PRIORITÄT - Langfristig

### 10. UI/UX Verbesserungen
- Dark Mode Toggle (CSS-Variablen)
- Lade-indikatoren für API-Aufrufe
- Tooltip-Hilfen für erste Benutzer

---

## 📦 Abhängigkeiten aktualisieren

| Paket | Aktuelle Version | Ziel-Version | Grund |
|-------|------------------|--------------|-------|
| playwright | 1.40.0+ | Latest Stable | Chrome-Kompatibilität |
| yt-dlp | Latest | Latest | Verbesserte HLS-Unterstützung |
| flask | 3.0.0+ | Latest | Sicherheits-Patches |
| eventlet | 0.33.0+ | Latest | Performance-Verbesserungen |

---

## 🚀 Rollout Strategie

### Phase 1 (Sofort - 24h): Kritische Fixes
- Memory-Leak Fix in BrowserPool
- Popup-Behandlung für 18+ Seiten
- Parallelismus-Limit Dynamisierung

### Phase 2 (1 Woche): High-Priority Features
- Background Scraper Thread-Pool
- Batch-Download Parallelisierung
- Drag-and-Drop Queue Support

### Phase 3 (2 Wochen): Medium-Priority
- Ad-Blocking Update-Mechanismus
- Verbessertes Cover Caching
- Graceful Fallbacks

---

## 🧪 Testing Strategie

1. **Integrationstests**: Längerer Download-Simulation (>2h)
2. **Stresstests**: 50+ parallel Downloads
3. **Memory-Leak Tests**: 10 Stunden Dauerlauf mit GC-Monitoring
4. **Fallback-Tests**: Browser-Töten während laufendem Download

---

## 📝 Notizen für Developer

[ ] Alle Änderungen müssen rückwärts-kompatibel sein
[ ] JSON-Schema Versionierung für Queue-Items
[ ] API-Versionierung (`/api/v2/` für neue Endpunkte)
[ ] Comprehensive Logging mit correlation IDs
