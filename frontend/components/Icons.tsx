// Iconos custom en el estilo Colsubsidio (geométricos, amarillos con outline oscuro)

interface IconProps {
  size?: number;
  className?: string;
  color?: string;
  strokeColor?: string;
}

const DEFAULTS = {
  size: 24,
  color: "#FFD000",      // bAmarillo
  strokeColor: "#1F2937", // gris oscuro
};

// ─── K Colsubsidio (logo mark) ───────────────────────────────────────────
export function KMarkIcon({
  size = DEFAULTS.size,
  color = DEFAULTS.color,
  strokeColor = DEFAULTS.strokeColor,
  className,
}: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
    >
      {/* Barra vertical (cuerpo de la K) */}
      <rect x="4" y="2" width="5" height="28" fill={color} />
      {/* Brazo superior (triángulo) */}
      <polygon points="9,16 22,2 28,2 14,16" fill={color} />
      {/* Brazo inferior (triángulo) */}
      <polygon points="9,16 22,30 28,30 14,16" fill={color} />
      {/* Líneas de detalle oscuras (estilo Colsubsidio) */}
      <line x1="9" y1="16" x2="14" y2="16" stroke={strokeColor} strokeWidth="1" />
      <line x1="14" y1="16" x2="22" y2="2" stroke={strokeColor} strokeWidth="0.5" />
      <line x1="14" y1="16" x2="22" y2="30" stroke={strokeColor} strokeWidth="0.5" />
    </svg>
  );
}

// ─── Micrófono (Voz) ─────────────────────────────────────────────────────
export function MicIcon({
  size = DEFAULTS.size,
  color = DEFAULTS.color,
  strokeColor = DEFAULTS.strokeColor,
  className,
}: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
    >
      {/* Cuerpo del micro */}
      <rect
        x="11"
        y="4"
        width="10"
        height="16"
        rx="5"
        fill={color}
        stroke={strokeColor}
        strokeWidth="1.8"
      />
      {/* Líneas de la rejilla */}
      <line x1="13" y1="9" x2="19" y2="9" stroke={strokeColor} strokeWidth="1.8" strokeLinecap="round" />
      <line x1="13" y1="13" x2="19" y2="13" stroke={strokeColor} strokeWidth="1.8" strokeLinecap="round" />
      <line x1="13" y1="17" x2="19" y2="17" stroke={strokeColor} strokeWidth="1.8" strokeLinecap="round" />
      {/* Arco de soporte */}
      <path
        d="M7 17 Q7 24 16 24 Q25 24 25 17"
        stroke={strokeColor}
        strokeWidth="1.8"
        fill="none"
        strokeLinecap="round"
      />
      {/* Base */}
      <line x1="16" y1="24" x2="16" y2="28" stroke={strokeColor} strokeWidth="1.8" strokeLinecap="round" />
      <line x1="12" y1="28" x2="20" y2="28" stroke={strokeColor} strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

// ─── Teclado (Manual) ────────────────────────────────────────────────────
export function KeyboardIcon({
  size = DEFAULTS.size,
  color = DEFAULTS.color,
  strokeColor = DEFAULTS.strokeColor,
  className,
}: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
    >
      {/* Cuerpo del teclado */}
      <rect
        x="3"
        y="9"
        width="26"
        height="14"
        rx="2"
        fill={color}
        stroke={strokeColor}
        strokeWidth="1.8"
      />
      {/* Fila superior de teclas */}
      <rect x="6" y="12" width="3" height="3" rx="0.5" fill={strokeColor} />
      <rect x="11" y="12" width="3" height="3" rx="0.5" fill={strokeColor} />
      <rect x="16" y="12" width="3" height="3" rx="0.5" fill={strokeColor} />
      <rect x="21" y="12" width="3" height="3" rx="0.5" fill={strokeColor} />
      {/* Spacebar */}
      <rect x="9" y="17" width="14" height="3" rx="0.5" fill={strokeColor} />
    </svg>
  );
}

// ─── Lupa (Buscar) ───────────────────────────────────────────────────────
export function SearchIcon({
  size = DEFAULTS.size,
  color = DEFAULTS.color,
  strokeColor = DEFAULTS.strokeColor,
  className,
}: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
    >
      {/* Lente */}
      <circle
        cx="14"
        cy="14"
        r="8"
        fill={color}
        stroke={strokeColor}
        strokeWidth="2"
      />
      {/* Detalle interior (vidrio) */}
      <circle cx="14" cy="14" r="3" fill={strokeColor} opacity="0.15" />
      {/* Mango */}
      <line
        x1="20"
        y1="20"
        x2="27"
        y2="27"
        stroke={strokeColor}
        strokeWidth="2.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

// ─── Stop (Finalizar) ────────────────────────────────────────────────────
export function StopIcon({
  size = DEFAULTS.size,
  color = DEFAULTS.color,
  strokeColor = DEFAULTS.strokeColor,
  className,
}: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
    >
      <rect
        x="8"
        y="8"
        width="16"
        height="16"
        rx="2"
        fill={color}
        stroke={strokeColor}
        strokeWidth="2"
      />
    </svg>
  );
}

// ─── Descargar (Exportar) ────────────────────────────────────────────────
export function DownloadIcon({
  size = DEFAULTS.size,
  color = DEFAULTS.color,
  strokeColor = DEFAULTS.strokeColor,
  className,
}: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
    >
      {/* Flecha */}
      <path
        d="M16 4 L16 20 M9 14 L16 21 L23 14"
        stroke={strokeColor}
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
      {/* Bandeja */}
      <rect
        x="5"
        y="24"
        width="22"
        height="4"
        rx="1"
        fill={color}
        stroke={strokeColor}
        strokeWidth="2"
      />
    </svg>
  );
}

// ─── Cerrar (X) ──────────────────────────────────────────────────────────
export function CloseIcon({
  size = DEFAULTS.size,
  strokeColor = DEFAULTS.strokeColor,
  className,
}: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
    >
      <path
        d="M8 8 L24 24 M24 8 L8 24"
        stroke={strokeColor}
        strokeWidth="2.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

// ─── Chevron ─────────────────────────────────────────────────────────────
export function ChevronIcon({
  size = 16,
  direction = "down",
  color = DEFAULTS.strokeColor,
  className,
}: IconProps & { direction?: "down" | "up" | "left" | "right" }) {
  const rotation = { down: 0, up: 180, left: 90, right: -90 }[direction];
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ transform: `rotate(${rotation}deg)` }}
    >
      <path
        d="M6 9l6 6 6-6"
        stroke={color}
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
