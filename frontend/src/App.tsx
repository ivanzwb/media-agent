import { Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Sources from "./pages/Sources";
import Drafts from "./pages/Drafts";
import Archive from "./pages/Archive";
import Settings from "./pages/Settings";
import Placeholder from "./pages/Placeholder";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/archive" element={<Archive />} />
        <Route path="/drafts" element={<Drafts />} />
        <Route path="/drafts/:id/edit" element={<Placeholder title="编辑草稿" />} />
        <Route path="/sources" element={<Sources />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Placeholder title="未找到页面" />} />
      </Route>
    </Routes>
  );
}
