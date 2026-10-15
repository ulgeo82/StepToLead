/* eslint-disable @next/next/no-img-element */
// Логотип StepToLead (wordmark, прозрачный PNG из фирменного макета).
// tone="light" — для тёмного фона (белое «StepTo», синее «Lead»), tone="dark" — для белого фона (тёмно-синее «StepTo»).
export function BrandMark({ tone = "dark", size = 34 }: { tone?: "light" | "dark"; size?: number }) {
  return <span className={`stlBrand ${tone}`}>
    <img src={`/brand/steptolead-wordmark-${tone === "light" ? "light" : "dark"}.png`} alt="StepToLead" height={size} style={{ width: "auto", height: size }} />
  </span>;
}
