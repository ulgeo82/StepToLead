import Link from "next/link";
export default function Logo() {
  return <Link href="/admin" className="brand" aria-label="StepToLead — управление"><span className="brandMark"><i /><i /><i /></span><span className="brandText">StepToLead</span></Link>;
}
