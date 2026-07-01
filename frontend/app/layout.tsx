import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Trade-Dash",
  description: "Unified Trading Signal Dashboard — News + Signal Correlation & Research Engine",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
