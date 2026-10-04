// Fonts are self-hosted from the `geist` package, so builds never call Google Fonts.
import { GeistMono } from "geist/font/mono";
import { GeistSans } from "geist/font/sans";
import type { Metadata } from "next";

import { SiteHeader } from "@/components/SiteHeader";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "ORM_AI", template: "%s · ORM_AI" },
  description: "A multi-agent AI assistant that keeps local shops' records organised.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col">
        <SiteHeader />
        <main className="flex-1">{children}</main>
        <footer className="border-t border-stone-200 py-6 text-center text-xs text-stone-500 dark:border-stone-800">
          ORM_AI · Phase 0 skeleton
        </footer>
      </body>
    </html>
  );
}
