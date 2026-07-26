import Link from "next/link";

interface LogoProps {
  size?: "sm" | "md" | "lg" | "xl";
  href?: string | null;
  className?: string;
  /** Muestra el logo de B-Link junto al de Colsubsidio (co-branding). */
  showBLink?: boolean;
}

/**
 * Logo de Colsubsidio + B-Link (co-branding).
 * Colsubsidio: /public/logo.png — B-Link: /public/b-link-logo.png.
 *
 * El alto es fijo por tamaño; el ancho se ajusta al aspect ratio
 * natural de cada imagen (así no queda espacio vacío si la imagen es cuadrada).
 * B-Link se muestra un poco más chico y separado por un divisor sutil,
 * como marca secundaria — sin reemplazar ni empequeñecer a Colsubsidio.
 */
export function Logo({
  size = "sm",
  href = "/",
  className = "",
  showBLink = true,
}: LogoProps) {
  // Solo definimos el alto; el ancho lo maneja la imagen
  const heights = {
    sm: 40,
    md: 56,
    lg: 80,
    xl: 112,
  };
  const h = heights[size];
  const hBLink = Math.round(h * 0.68);

  const content = (
    <span className={`inline-flex items-center gap-3 ${className}`}>
      <img
        src="/logo.png?v=3"
        alt="Colsubsidio"
        draggable={false}
        style={{
          height: `${h}px`,
          width: "auto",          // ← clave: respeta el aspect ratio natural
          display: "block",
        }}
        onError={(e) => {
          const target = e.currentTarget;
          target.style.display = "none";
          const parent = target.parentElement;
          if (parent && !parent.querySelector(".logo-fallback")) {
            const fb = document.createElement("div");
            fb.className =
              "logo-fallback bg-bAmarillo rounded flex items-center justify-center font-bold text-bAzul";
            fb.style.cssText = `height:${h}px;width:${h}px;font-size:${h / 2}px`;
            fb.textContent = "C";
            parent.appendChild(fb);
          }
        }}
      />
      {showBLink && (
        <>
          <span
            aria-hidden
            style={{
              width: 1,
              height: h * 0.6,
              background: "rgba(255,255,255,0.35)",
              flexShrink: 0,
            }}
          />
          <img
            src="/b-link-logo.png?v=1"
            alt="B-Link"
            draggable={false}
            style={{
              height: `${hBLink}px`,
              width: "auto",
              display: "block",
            }}
            onError={(e) => {
              e.currentTarget.style.display = "none";
            }}
          />
        </>
      )}
    </span>
  );

  if (href) {
    return (
      <Link href={href} className="inline-flex items-center">
        {content}
      </Link>
    );
  }
  return content;
}
