import Link from "next/link";

interface LogoProps {
  size?: "sm" | "md" | "lg" | "xl";
  href?: string | null;
  className?: string;
}

/**
 * Logo de Colsubsidio.
 * La imagen debe estar en /public/logo.png.
 *
 * El alto es fijo por tamaño; el ancho se ajusta al aspect ratio
 * natural de la imagen (así no queda espacio vacío si la imagen es cuadrada).
 */
export function Logo({
  size = "sm",
  href = "/",
  className = "",
}: LogoProps) {
  // Solo definimos el alto; el ancho lo maneja la imagen
  const heights = {
    sm: 40,
    md: 56,
    lg: 80,
    xl: 112,
  };
  const h = heights[size];

  const content = (
    <img
      src="/logo.png?v=3"
      alt="Colsubsidio"
      className={className}
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
