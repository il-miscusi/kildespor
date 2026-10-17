import "./globals.css"
export const metadata = { title: 'Kildespor — Follow the source', description: 'Evidence-backed profiles of Norwegian companies.', icons: { icon: 'https://keystone.jboxai.com/project-assets/hwvs3ablfa1urrhnvfjc5pwn/z81d0le07ei8suqvrgla5r77/564a40dbdd02d29c0ad6db631aad40460ecd75f2cb5ce86370592094d429c683/564a40dbdd02d29c.svg' } };

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="en">
      <body>
        {children}
      </body>
    </html>
  )
}
