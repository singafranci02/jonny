import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Jonny · Life HQ",
  description: "Reminders, updates and an agent per life area",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
