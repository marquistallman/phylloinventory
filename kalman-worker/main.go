// kalman-worker: consume la cola pending_evaluations, evalua con
// kalman_evaluar() y aplica o marca como sospechosa.
//
// Paralelismo: pool de goroutines workers que pelean por filas con
// SELECT ... FOR UPDATE SKIP LOCKED. Acelerador opcional por LISTEN/NOTIFY.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"sync"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Config struct {
	DBURL        string
	Workers      int
	PollInterval time.Duration
	HTTPAddr     string
	WorkerID     string
}

func loadConfig() Config {
	c := Config{
		DBURL:        getenv("DATABASE_URL", "postgres://cactus:cactus@postgres:5432/inventario?sslmode=disable"),
		Workers:      getenvInt("KALMAN_WORKERS", runtime.NumCPU()*2),
		PollInterval: getenvDuration("POLL_INTERVAL", 200*time.Millisecond),
		HTTPAddr:     getenv("HTTP_ADDR", ":8300"),
		WorkerID:     getenv("WORKER_ID", fmt.Sprintf("kw-%d", os.Getpid())),
	}
	return c
}

func getenv(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func getenvInt(k string, d int) int {
	if v := os.Getenv(k); v != "" {
		var n int
		fmt.Sscanf(v, "%d", &n)
		if n > 0 {
			return n
		}
	}
	return d
}

func getenvDuration(k string, d time.Duration) time.Duration {
	if v := os.Getenv(k); v != "" {
		if dur, err := time.ParseDuration(v); err == nil {
			return dur
		}
	}
	return d
}

// =====================================================================
//  Pendiente (fila de la cola)
// =====================================================================

type Pending struct {
	ID          int64
	SessionID   string
	ToolName    string
	ProductoID  int32
	Tipo        string
	Cantidad    float64
	Payload     []byte
}

// =====================================================================
//  Resultado de kalman_evaluar()
// =====================================================================

type KalmanResult struct {
	Decision        string  `json:"decision"`
	Residual        float64 `json:"residual"`
	Umbral          float64 `json:"umbral"`
	MediaActual     float64 `json:"media_actual"`
	VarianzaActual  float64 `json:"varianza_actual"`
	StockProyectado float64 `json:"stock_proyectado"`
	PuntajeRiesgo   float64 `json:"puntaje_riesgo"`
}

// =====================================================================
//  Pool de workers
// =====================================================================

type WorkerPool struct {
	cfg    Config
	pool   *pgxpool.Pool
	wg     sync.WaitGroup
	ctx    context.Context
	cancel context.CancelFunc

	processed  uint64
	accepted   uint64
	suspicious uint64
	confirmed  uint64
	rejected   uint64
	errors     uint64
	muStats    sync.Mutex
}

func NewWorkerPool(cfg Config) (*WorkerPool, error) {
	pcfg, err := pgxpool.ParseConfig(cfg.DBURL)
	if err != nil {
		return nil, fmt.Errorf("parse db url: %w", err)
	}
	pcfg.MaxConns = int32(cfg.Workers + 2)
	pcfg.MinConns = 1

	pool, err := pgxpool.NewWithConfig(context.Background(), pcfg)
	if err != nil {
		return nil, fmt.Errorf("connect pool: %w", err)
	}

	if err := pool.Ping(context.Background()); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping db: %w", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	return &WorkerPool{cfg: cfg, pool: pool, ctx: ctx, cancel: cancel}, nil
}

func (wp *WorkerPool) Start() {
	log.Printf("[kalman-worker %s] starting %d workers", wp.cfg.WorkerID, wp.cfg.Workers)

	//  Suscripcion LISTEN/NOTIFY para reducir latencia
	go wp.listenNotify()

	for i := 0; i < wp.cfg.Workers; i++ {
		wp.wg.Add(1)
		go wp.worker(i)
	}
}

func (wp *WorkerPool) Shutdown() {
	log.Printf("[kalman-worker %s] shutting down...", wp.cfg.WorkerID)
	wp.cancel()
	wp.wg.Wait()
	wp.pool.Close()
}

func (wp *WorkerPool) Stats() (uint64, uint64, uint64, uint64, uint64, uint64) {
	wp.muStats.Lock()
	defer wp.muStats.Unlock()
	return wp.processed, wp.accepted, wp.suspicious, wp.confirmed, wp.rejected, wp.errors
}

func (wp *WorkerPool) incStat(field *uint64) {
	wp.muStats.Lock()
	*field++
	wp.muStats.Unlock()
}

// listenNotify: despertador opcional via LISTEN/NOTIFY. Los workers
// ya pollan, asi que esto es solo un acelerador de latencia. Si falla,
// los workers siguen funcionando.
func (wp *WorkerPool) listenNotify() {
	for {
		select {
		case <-wp.ctx.Done():
			return
		default:
		}

		conn, err := wp.pool.Acquire(wp.ctx)
		if err != nil {
			log.Printf("[listen] acquire err: %v", err)
			time.Sleep(time.Second)
			continue
		}

		_, err = conn.Exec(wp.ctx, "LISTEN pending_eval_channel")
		if err != nil {
			log.Printf("[listen] LISTEN err: %v", err)
			conn.Release()
			time.Sleep(time.Second)
			continue
		}

		for {
			notif, err := conn.Conn().WaitForNotification(wp.ctx)
			if err != nil {
				if wp.ctx.Err() != nil {
					conn.Release()
					return
				}
				log.Printf("[listen] wait err: %v", err)
				break
			}
			log.Printf("[notify] new pending id=%s channel=%s", notif.Payload, notif.Channel)
			//  Los workers pollan con SKIP LOCKED, asi que la proxima
			//  iteracion pillara la fila. NOTIFY solo reduce latencia
			//  al permitirles salir del sleep antes.
		}
		conn.Release()
	}
}

func (wp *WorkerPool) worker(id int) {
	defer wp.wg.Done()
	log.Printf("[worker %d] started", id)

	ticker := time.NewTicker(wp.cfg.PollInterval)
	defer ticker.Stop()

	for {
		select {
		case <-wp.ctx.Done():
			log.Printf("[worker %d] stopping", id)
			return
		case <-ticker.C:
			for {
				processed, err := wp.processOne(id)
				if err != nil {
					log.Printf("[worker %d] error: %v", id, err)
					wp.incStat(&wp.errors)
					break
				}
				if !processed {
					break
				}
			}
		}
	}
}

// processOne: agarra una fila PENDING (con SKIP LOCKED) y la procesa
// DENTRO de la misma transaccion. El FOR UPDATE se mantiene hasta el
// commit: si comitearamos el lock antes de procesar, otra goroutine
// podria tomar la MISMA fila (status sigue PENDING) y aplicar el
// movimiento dos veces.
// Devuelve true si proceso algo, false si no habia nada.
func (wp *WorkerPool) processOne(workerID int) (bool, error) {
	tx, err := wp.pool.BeginTx(wp.ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return false, fmt.Errorf("begin: %w", err)
	}
	//  Si algo falla, rollback: la fila vuelve a PENDING y se reintenta.
	defer tx.Rollback(wp.ctx)

	var p Pending
	//  NOTA: producto_id/cantidad son NULL en filas de confirmar_movimiento;
	//  sin COALESCE el scan a int32 falla y la fila envenena la cola
	//  (ORDER BY id -> siempre es la primera -> head-of-line blocking).
	err = tx.QueryRow(wp.ctx, `
		SELECT id, COALESCE(session_id,''), tool_name,
		       COALESCE(producto_id,0), COALESCE(tipo,''), COALESCE(cantidad,0),
		       payload::text
		FROM pending_evaluations
		WHERE status = 'PENDING'
		ORDER BY id
		FOR UPDATE SKIP LOCKED
		LIMIT 1
	`).Scan(&p.ID, &p.SessionID, &p.ToolName, &p.ProductoID, &p.Tipo, &p.Cantidad, &p.Payload)
	if err != nil {
		if err == pgx.ErrNoRows {
			return false, nil
		}
		return false, fmt.Errorf("select pending: %w", err)
	}

	//  Marcar como locked (visibilidad para debug/operaciones)
	_, _ = tx.Exec(wp.ctx, `
		UPDATE pending_evaluations SET locked_by=$1, locked_at=NOW() WHERE id=$2
	`, wp.cfg.WorkerID, p.ID)

	log.Printf("[worker %d] picked pending id=%d tool=%s", workerID, p.ID, p.ToolName)
	wp.incStat(&wp.processed)

	//  Procesar segun tool_name (todo dentro de tx)
	var procErr error
	switch p.ToolName {
	case "agregar_inventario", "remover_inventario":
		procErr = wp.evalMovimiento(tx, p)
	case "confirmar_movimiento":
		procErr = wp.evalConfirmacion(tx, p)
	case "consultar_inventario", "investigar_sospechosos":
		//  Las lecturas las responde el LLM-service directo, no la cola.
		//  Si caen aqui es porque alguien las encolo por error: las marcamos
		//  como ACEPTADA para que no se reintenten.
		_, procErr = tx.Exec(wp.ctx, `
			UPDATE pending_evaluations
			SET status='ACEPTADA', resolved_at=NOW()
			WHERE id=$1
		`, p.ID)
	default:
		log.Printf("[worker %d] unknown tool %s, dropping", workerID, p.ToolName)
		_, procErr = tx.Exec(wp.ctx, `
			UPDATE pending_evaluations
			SET status='RECHAZADA', resolved_at=NOW(), decision='unknown_tool'
			WHERE id=$1
		`, p.ID)
	}
	if procErr != nil {
		return false, procErr
	}

	if err := tx.Commit(wp.ctx); err != nil {
		return false, fmt.Errorf("commit: %w", err)
	}
	return true, nil
}

// evalMovimiento: llama kalman_evaluar(). Si PASA, aplica. Si FALLA, marca sospechosa.
// Todo dentro de tx: el row-lock de la fila pending se mantiene hasta el commit.
func (wp *WorkerPool) evalMovimiento(tx pgx.Tx, p Pending) error {
	var k KalmanResult
	err := tx.QueryRow(wp.ctx, `
		SELECT decision, residual, umbral, media_actual, varianza_actual, stock_proyectado, puntaje_riesgo
		FROM kalman_evaluar($1, $2, $3)
	`, p.ProductoID, p.Tipo, p.Cantidad).Scan(
		&k.Decision, &k.Residual, &k.Umbral,
		&k.MediaActual, &k.VarianzaActual, &k.StockProyectado, &k.PuntajeRiesgo,
	)
	if err != nil {
		return fmt.Errorf("kalman_evaluar: %w", err)
	}

	log.Printf("[pending %d] kalman decision=%s residual=%.2f umbral=%.2f puntaje=%.2f",
		p.ID, k.Decision, k.Residual, k.Umbral, k.PuntajeRiesgo)

	switch k.Decision {
	case "PASA":
		var movID int32
		err := tx.QueryRow(wp.ctx, `
			SELECT aplicar_movimiento_aceptado($1, $2, $3, $4, $5)
		`, p.ProductoID, p.Tipo, p.Cantidad, k.Residual, k.Umbral).Scan(&movID)
		if err != nil {
			return fmt.Errorf("aplicar_movimiento: %w", err)
		}

		_, err = tx.Exec(wp.ctx, `
			UPDATE pending_evaluations
			SET status='ACEPTADA', decision=$1, residual=$2, umbral=$3, movimiento_id=$4, resolved_at=NOW()
			WHERE id=$5
		`, k.Decision, k.Residual, k.Umbral, movID, p.ID)
		if err != nil {
			return err
		}

		//  Sincronizar registros_conteo
		_, _ = tx.Exec(wp.ctx, `
			UPDATE registros_conteo SET decision_kalman='ACEPTADA', movimiento_id=$1
			WHERE pending_id=$2
		`, movID, p.ID)

		//  Auditoria: log para investigar_sospechosos
		_, _ = tx.Exec(wp.ctx, `
			INSERT INTO auditoria_log (movimiento_id, puntaje_riesgo, motivo)
			VALUES ($1, $2, $3)
		`, movID, k.PuntajeRiesgo,
			fmt.Sprintf("ACEPTADA residual=%.2f umbral=%.2f (%.2fσ)", k.Residual, k.Umbral, k.PuntajeRiesgo))

		wp.incStat(&wp.accepted)
		log.Printf("[pending %d] ACEPTADA movimiento_id=%d", p.ID, movID)

	case "FALLA":
		_, err := tx.Exec(wp.ctx, `
			UPDATE pending_evaluations
			SET status='SOSPECHOSA', decision=$1, residual=$2, umbral=$3, resolved_at=NULL
			WHERE id=$4
		`, k.Decision, k.Residual, k.Umbral, p.ID)
		if err != nil {
			return err
		}
		//  Sincronizar registros_conteo
		_, _ = tx.Exec(wp.ctx, `
			UPDATE registros_conteo SET decision_kalman='SOSPECHOSA'
			WHERE pending_id=$1
		`, p.ID)
		wp.incStat(&wp.suspicious)
		log.Printf("[pending %d] SOSPECHOSA — esperando confirmacion humana", p.ID)

	case "ERROR":
		_, err := tx.Exec(wp.ctx, `
			UPDATE pending_evaluations
			SET status='RECHAZADA', decision='kalman_error', resolved_at=NOW()
			WHERE id=$1
		`, p.ID)
		if err != nil {
			return err
		}
	default:
		return fmt.Errorf("unknown kalman decision: %s", k.Decision)
	}

	return nil
}

// evalConfirmacion: extrae {pending_id, confirmar} del payload y resuelve (dentro de tx).
func (wp *WorkerPool) evalConfirmacion(tx pgx.Tx, p Pending) error {
	var args struct {
		PendingID  int64 `json:"pending_id"`
		Confirmar  bool  `json:"confirmar"`
	}
	if err := json.Unmarshal(p.Payload, &args); err != nil {
		return fmt.Errorf("parse confirm payload: %w", err)
	}
	if args.PendingID == 0 {
		//  Compat: el campo puede venir como movimiento_id (legacy)
		var alt struct {
			MovimientoID int64 `json:"movimiento_id"`
			Confirmar    bool  `json:"confirmar"`
		}
		if err := json.Unmarshal(p.Payload, &alt); err == nil && alt.MovimientoID != 0 {
			args.PendingID = alt.MovimientoID
		}
	}

	var result string
	err := tx.QueryRow(wp.ctx, `
		SELECT confirmar_movimiento($1, $2)
	`, args.PendingID, args.Confirmar).Scan(&result)
	if err != nil {
		return fmt.Errorf("confirmar_movimiento: %w", err)
	}

	//  Marcar el pending de la confirmacion como resuelto
	_, err = tx.Exec(wp.ctx, `
		UPDATE pending_evaluations
		SET status=$1, resolved_at=NOW()
		WHERE id=$2
	`, ternary(args.Confirmar, "CONFIRMADA_MANUAL", "RECHAZADA"), p.ID)
	if err != nil {
		return err
	}

	if args.Confirmar {
		wp.incStat(&wp.confirmed)
		//  Sincronizar registros_conteo del pending original
		_, _ = tx.Exec(wp.ctx, `
			UPDATE registros_conteo SET decision_kalman='CONFIRMADA_MANUAL'
			WHERE pending_id=$1
		`, args.PendingID)
	} else {
		wp.incStat(&wp.rejected)
		_, _ = tx.Exec(wp.ctx, `
			UPDATE registros_conteo SET decision_kalman='RECHAZADA'
			WHERE pending_id=$1
		`, args.PendingID)
	}
	log.Printf("[pending %d] confirm resolved: %s", p.ID, result)

	//  Guardar el mensaje en payload para que la CLI lo recoja
	_, _ = tx.Exec(wp.ctx, `
		UPDATE pending_evaluations
		SET payload = jsonb_set(payload, '{result}', to_jsonb($1::text))
		WHERE id=$2
	`, result, p.ID)

	return nil
}

func ternary[T any](b bool, a, c T) T {
	if b {
		return a
	}
	return c
}

// =====================================================================
//  HTTP server: /health y /stats
// =====================================================================

func (wp *WorkerPool) StartHTTP() {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := wp.pool.Ping(ctx); err != nil {
			http.Error(w, "db down: "+err.Error(), 503)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		processed, accepted, suspicious, confirmed, rejected, errs := wp.Stats()
		json.NewEncoder(w).Encode(map[string]any{
			"status":     "ok",
			"worker_id":  wp.cfg.WorkerID,
			"workers":    wp.cfg.Workers,
			"processed":  processed,
			"accepted":   accepted,
			"suspicious": suspicious,
			"confirmed":  confirmed,
			"rejected":   rejected,
			"errors":     errs,
		})
	})
	mux.HandleFunc("/stats", func(w http.ResponseWriter, r *http.Request) {
		processed, accepted, suspicious, confirmed, rejected, errs := wp.Stats()
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]uint64{
			"processed":  processed,
			"accepted":   accepted,
			"suspicious": suspicious,
			"confirmed":  confirmed,
			"rejected":   rejected,
			"errors":     errs,
		})
	})

	log.Printf("[kalman-worker] HTTP listening on %s", wp.cfg.HTTPAddr)
	if err := http.ListenAndServe(wp.cfg.HTTPAddr, mux); err != nil {
		log.Printf("http server: %v", err)
	}
}

// =====================================================================
//  main
// =====================================================================

func main() {
	cfg := loadConfig()
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Printf("config: workers=%d poll=%s db=%s", cfg.Workers, cfg.PollInterval, redactDB(cfg.DBURL))

	wp, err := NewWorkerPool(cfg)
	if err != nil {
		log.Fatalf("init pool: %v", err)
	}

	wp.Start()
	go wp.StartHTTP()

	//  Shutdown limpio
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh
	wp.Shutdown()
}

func redactDB(url string) string {
	//  postgres://user:pass@host:port/db -> postgres://user:***@host:port/db
	at := -1
	colon := -1
	for i := 0; i < len(url); i++ {
		if url[i] == '@' && at == -1 {
			at = i
		}
		if url[i] == ':' && colon == -1 && i > 8 {
			colon = i
		}
	}
	if at > 0 && colon > 0 && colon < at {
		return url[:colon+1] + "***" + url[at:]
	}
	return url
}
