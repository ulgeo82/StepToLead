import Image from "next/image";
import Link from "next/link";

// Знак — public/brand/steptolead-mark-*.png (прозрачный фон), надпись — текстом, чтобы была резкой.
export default function Logo() {
  return <Link href="/admin" className="brand" aria-label="Steptolead — главная админки">
    <Image src="/brand/steptolead-mark-128.png" alt="" width={40} height={40} priority className="brandLogo" />
    <span className="brandText">Step<span className="brandAccent">to</span>lead</span>
  </Link>;
}
