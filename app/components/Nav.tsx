import Link from "next/link";

export function Nav({ active }: { active: "home" | "talk" | "setup" }) {
  const link = (href: string, key: typeof active, label: string) => (
    <Link href={href} className={active === key ? "nav-link active" : "nav-link"}>
      {label}
    </Link>
  );
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-orb" aria-hidden />
        <span>Jonny</span>
        <span className="brand-sub">Life HQ</span>
      </div>
      <nav>
        {link("/", "home", "Dashboard")}
        {link("/talk", "talk", "Talk")}
        {link("/setup", "setup", "Setup")}
      </nav>
    </header>
  );
}
