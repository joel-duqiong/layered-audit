import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.lang.reflect.Array;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

class GenericFileHarnessObserve extends GenericFileHarnessDetectors {
    // ---- 方法 ----
    static JsonNode applyView(String viewName, Object subject) {
        JsonNode v = VIEWS.path(viewName);
        if (v.isMissingNode()) {
            return M.getNodeFactory().nullNode();
        }
        Object val = readPath(subject, v.path("source").asText(""));
        if (v.path("list").asBoolean(false) && val instanceof Collection<?> c) {
            ArrayNode arr = M.createArrayNode();
            for (Object o : c) {
                arr.add(projectFields(o, v.path("fields")));
            }
            return arr;
        }
        if (v.has("fields")) {
            return projectFields(val, v.path("fields"));
        }
        return toJson(val);
    }

    static JsonNode projectFields(Object o, JsonNode fields) {
        ObjectNode n = M.createObjectNode();
        var it = fields.fields();
        while (it.hasNext()) {
            var e = it.next();
            n.set(e.getKey(), toJson(readPath(o, e.getValue().asText(""))));
        }
        return n;
    }

    static JsonNode measure(JsonNode spec, ObjectNode res) {
        String op = spec.path("op").asText("value");
        String of = spec.path("of").asText("");
        Object value = resolveMeasureSource(of, res);
        String path = spec.path("path").asText("");
        if (!path.isEmpty()) {
            value = readPath(value, path);
        }
        switch (op) {
            case "count" -> {
                return M.getNodeFactory().numberNode(sizeOf(value));
            }
            case "ends_with" -> {
                String s = String.valueOf(value);
                return M.getNodeFactory().booleanNode(s.endsWith(spec.path("suffix").asText("")));
            }
            case "contains" -> {
                String s = String.valueOf(value);
                return M.getNodeFactory().booleanNode(s.contains(spec.path("text").asText("")));
            }
            case "contains_all" -> {
                String s = String.valueOf(value);
                boolean all = true;
                for (JsonNode t : spec.path("texts")) {
                    if (!s.contains(t.asText())) {
                        all = false;
                    }
                }
                return M.getNodeFactory().booleanNode(all);
            }
            case "any_eq" -> {
                String field = spec.path("field").asText("");
                JsonNode want = spec.path("value");
                boolean hit = false;
                if (value instanceof Collection<?> c) {
                    for (Object o : c) {
                        Object f = readPath(o, field);
                        if (f != null && want != null && String.valueOf(f).equals(want.asText())) {
                            hit = true;
                            break;
                        }
                    }
                }
                return M.getNodeFactory().booleanNode(hit);
            }
            case "lookup" -> {
                String key = spec.path("key").asText("");
                String field = spec.path("field").asText("");
                JsonNode want = spec.path("value");
                Object found = null;
                if (value instanceof Collection<?> c) {
                    for (Object o : c) {
                        Object k = readPath(o, key);
                        if (k != null && want != null && String.valueOf(k).equals(want.asText())) {
                            found = readPath(o, field);
                            break;
                        }
                    }
                }
                return toJson(found);
            }
            case "all_lookup" -> {
                String key = spec.path("key").asText("");
                String field = spec.path("field").asText("");
                boolean all = true;
                if (value instanceof Collection<?> c) {
                    for (JsonNode want : spec.path("values")) {
                        boolean hit = false;
                        for (Object o : c) {
                            Object k = readPath(o, key);
                            if (k != null && String.valueOf(k).equals(want.asText())) {
                                hit = Boolean.TRUE.equals(readPath(o, field));
                                break;
                            }
                        }
                        if (!hit) {
                            all = false;
                            break;
                        }
                    }
                } else {
                    all = false;
                }
                return M.getNodeFactory().booleanNode(all);
            }
            case "all_of" -> {
                boolean all = true;
                for (JsonNode name : spec.path("of")) {
                    JsonNode v = res.get(name.asText());
                    if (v == null || !v.asBoolean(false)) {
                        all = false;
                    }
                }
                return M.getNodeFactory().booleanNode(all);
            }
            case "not" -> {
                return M.getNodeFactory().booleanNode(!truthy(value));
            }
            case "is_null" -> {
                return M.getNodeFactory().booleanNode(value == null);
            }
            case "same_as" -> {
                Object other = resolveMeasureSource(spec.path("other").asText(""), res);
                return M.getNodeFactory().booleanNode(java.util.Objects.equals(value, other));
            }
            case "log_contains" -> {
                String text = spec.path("text").asText("");
                boolean hit = false;
                for (String line : capturedLogs()) {
                    if (line.contains(text)) {
                        hit = true;
                        break;
                    }
                }
                return M.getNodeFactory().booleanNode(hit);
            }
            default -> {
                return toJson(value);
            }
        }
    }

    static Object resolveMeasureSource(String of, ObjectNode res) {
        if ("return".equals(of)) {
            return LAST_RETURN;
        }
        if ("logs".equals(of)) {
            return capturedLogs();
        }
        JsonNode v = res.get(of);
        if (v == null || v.isNull()) {
            return null;
        }
        if (v.isArray() || v.isObject()) {
            return plain(v);
        }
        if (v.isNumber()) {
            return v.numberValue();
        }
        if (v.isBoolean()) {
            return v.booleanValue();
        }
        return v.asText();
    }

    static Object corsProbes(Object cfg, JsonNode origins) throws Exception {
        List<Object> out = new ArrayList<>();
        if (cfg == null) {
            return out;
        }
        Method checkOrigin = cfg.getClass().getMethod("checkOrigin", String.class);
        checkOrigin.setAccessible(true);
        for (JsonNode o : origins) {
            String origin = o.asText();
            Object allowed = checkOrigin.invoke(cfg, origin);
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("origin", origin);
            row.put("allowed", allowed != null);
            out.add(row);
        }
        return out;
    }

    static String resolveRelative(String baseUri, String relative) {
        try {
            Class<?> urlResource = Class.forName("org.springframework.core.io.UrlResource");
            Object r = urlResource.getConstructor(String.class).newInstance(baseUri);
            Method createRelative = urlResource.getMethod("createRelative", String.class);
            Object child = createRelative.invoke(r, relative);
            Method getUrl = child.getClass().getMethod("getURL");
            return String.valueOf(getUrl.invoke(child));
        } catch (Exception e) {
            return "";
        }
    }

}
