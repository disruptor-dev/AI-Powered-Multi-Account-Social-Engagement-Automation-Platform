import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Social Engagement Platform",
  description: "Manage social comments and engagement from one workspace.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
