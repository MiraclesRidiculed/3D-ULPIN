import "./globals.css";
import type { Metadata } from "next";
export const metadata: Metadata = { title: "V-CAD | 3D ULPIN", description: "Prototype 3D ULPIN & Volumetric Cadastre Platform" };
export default function RootLayout({children}:{children:React.ReactNode}) { return <html lang="en"><body>{children}</body></html>; }
