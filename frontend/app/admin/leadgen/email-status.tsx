import styles from "./leadgen.module.css";

const states: Record<string, string> = {
  valid: "✓ Проверена", risky: "⚠ Есть риск", invalid: "✕ Некорректная",
};
export function EmailStatus({ status }: { status?: string }) {
  return <span className={`${styles.badge} ${status === "valid" ? styles.fitYes
    : status === "invalid" ? styles.fitNo : styles.fitMaybe}`}>
    {states[status || ""] || "Не проверена"}</span>;
}
