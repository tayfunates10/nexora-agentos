import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Nexora AgentOS | Control plane", description: "Nexora AgentOS platform health and development status." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
