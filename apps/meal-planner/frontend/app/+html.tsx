import { ScrollViewStyleReset } from "expo-router/html";
import type { PropsWithChildren } from "react";

export default function HTML({ children }: PropsWithChildren) {
  return (
    <html lang="en">
      <head>
        <meta charSet="utf-8" />
        <meta httpEquiv="X-UA-Compatible" content="IE=edge" />
        <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no" />

        {/* PWA / mobile web */}
        <meta name="application-name" content="Meal Planner" />
        <meta name="apple-mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-status-bar-style" content="default" />
        <meta name="apple-mobile-web-app-title" content="Meal Planner" />
        <meta name="mobile-web-app-capable" content="yes" />
        <meta name="theme-color" content="#16a34a" />

        {/* Open Graph */}
        <meta property="og:title" content="Meal Planner" />
        <meta property="og:description" content="AI-powered meal planning with USDA nutrition data" />
        <meta property="og:type" content="website" />

        {/* PWA manifest */}
        <link rel="manifest" href="/manifest.json" />

        {/* Apple touch icon */}
        <link rel="apple-touch-icon" href="/assets/icon.png" />

        <title>Meal Planner</title>
        <ScrollViewStyleReset />
      </head>
      <body>{children}</body>
    </html>
  );
}
