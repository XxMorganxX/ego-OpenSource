import React from "react";
import ReactDOM from "react-dom/client";

// Fonts are bundled (OFL-1.1, see NOTICE), never fetched from a font CDN: the
// dashboard makes no third-party request. Same weights the old Google Fonts link asked for.
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";

import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
