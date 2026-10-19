import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import PdfEditor from "../PdfEditor.jsx";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <PdfEditor />
  </StrictMode>
);
