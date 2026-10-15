/* eslint-disable @next/next/no-img-element */
// Знак StepToLead (прозрачный PNG) + надпись текстом, чтобы была резкой на любом экране.
// tone="light" — для тёмного фона (белая «S»), tone="dark" — для белого фона (тёмно-синяя «S»).
export function BrandMark({ tone = "dark", size = 34 }: { tone?: "light" | "dark"; size?: number }) {
  return <span className={`stlBrand ${tone}`}>
    <img src={`/brand/steptolead-mark${tone === "dark" ? "-dark" : ""}-128.png`} alt="" width={size} height={size} />
    <span className="stlBrandText">Step<b>to</b>lead</span>
  </span>;
}
