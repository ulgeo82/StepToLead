import Image from "next/image";
import Link from "next/link";

// Логотип StepToLead (wordmark для тёмного фона), public/brand/steptolead-wordmark-light.png.
export default function Logo() {
  return <Link href="/admin" className="brand" aria-label="StepToLead — главная админки">
    <Image src="/brand/steptolead-wordmark-light.png" alt="StepToLead" width={772} height={158} priority className="brandLogo" />
  </Link>;
}
