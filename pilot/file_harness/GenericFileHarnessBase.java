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

class GenericFileHarnessBase {
    // ---- 共享状态 ----
    static final ObjectMapper M = new ObjectMapper().findAndRegisterModules();
    static JsonNode ROOT;
    static JsonNode STUBS;
    static JsonNode FIXTURES;
    static JsonNode CTOR;
    static JsonNode VIEWS;
    static JsonNode CASE_STUBS;
    static JsonNode CASE_FIXTURES;
    static JsonNode CASE_CTOR;
    static final Map<String, Object> FIXTURE_OBJECTS = new LinkedHashMap<>();
    static final Map<String, String> STUB_LABELS = new LinkedHashMap<>();
    static final Map<String, AtomicInteger> CALL_COUNTS = new ConcurrentHashMap<>();
    static final List<String> CALL_LOG = new ArrayList<>();
    static final Map<String, Object> CAPTURED = new LinkedHashMap<>();
    static Object TARGET;
    static Object LAST_RETURN;
    static Object LAST_RESPONSE;
    static final Map<Integer, Object> LAST_ARGS = new LinkedHashMap<>();
    static Path WORK_DIR;
    static Path CLASSES_ROOT;
    static Object logAppender;

    // ---- 方法 ----
    static Object defaultValue(Class<?> t) {
        if (t == Optional.class) {
            return Optional.empty();
        }
        if (t == boolean.class || t == Boolean.class) {
            return Boolean.FALSE;
        }
        if (t == int.class || t == Integer.class) {
            return 0;
        }
        if (t == long.class || t == Long.class) {
            return 0L;
        }
        if (t == double.class || t == Double.class) {
            return 0d;
        }
        if (t == float.class || t == Float.class) {
            return 0f;
        }
        if (t == short.class || t == Short.class) {
            return (short) 0;
        }
        if (t == byte.class || t == Byte.class) {
            return (byte) 0;
        }
        if (t == char.class || t == Character.class) {
            return (char) 0;
        }
        if (Collection.class.isAssignableFrom(t)) {
            return new ArrayList<>();
        }
        if (Map.class.isAssignableFrom(t)) {
            return new LinkedHashMap<>();
        }
        if (t == String.class) {
            return "";
        }
        return null;
    }

    /**
     * 模板展开：{"$t":"template","v":"名字"} 换成用例文件 templates 段里的内容。
     * 纯粹是为了让用例文件少写重复的请求体 —— 模板本身不含任何项目语义。
     */
    static JsonNode expandTemplates(JsonNode node) {
        if (node == null || node.isNull() || node.isValueNode()) {
            return node;
        }
        if (node.isObject()) {
            if (node.has("$t") && "template".equals(node.path("$t").asText())) {
                String name = node.path("v").asText();
                JsonNode t = ROOT == null ? null : ROOT.path("templates").path(name);
                if (t == null || t.isMissingNode()) {
                    throw new IllegalArgumentException("用例文件里没有这个模板: " + name);
                }
                return expandTemplates(t);
            }
            ObjectNode out = M.createObjectNode();
            var it = node.fields();
            while (it.hasNext()) {
                var e = it.next();
                out.set(e.getKey(), expandTemplates(e.getValue()));
            }
            return out;
        }
        ArrayNode arr = M.createArrayNode();
        node.forEach(e -> arr.add(expandTemplates(e)));
        return arr;
    }

    static JsonNode toJson(Object o) {
        if (o == null) {
            return M.getNodeFactory().nullNode();
        }
        try {
            return M.valueToTree(o);
        } catch (Exception e) {
            try {
                return reflectiveDump(o);
            } catch (Exception e2) {
                return M.getNodeFactory().textNode(String.valueOf(o));
            }
        }
    }

    static JsonNode reflectiveDump(Object o) throws Exception {
        ObjectNode n = M.createObjectNode();
        Class<?> k = o.getClass();
        if (k.isRecord()) {
            for (var c : k.getRecordComponents()) {
                try {
                    Method acc = c.getAccessor();
                    acc.setAccessible(true);
                    n.set(c.getName(), toJson(acc.invoke(o)));
                } catch (Exception ignored) {
                    // 读不到就跳过
                }
            }
            return n;
        }
        for (Method m : k.getMethods()) {
            if (m.getParameterCount() != 0 || m.getDeclaringClass() == Object.class) {
                continue;
            }
            String name = m.getName();
            if (name.startsWith("get") && name.length() > 3) {
                name = name.substring(3);
            } else if (name.startsWith("is") && name.length() > 2) {
                name = name.substring(2);
            } else {
                continue;
            }
            name = Character.toLowerCase(name.charAt(0)) + name.substring(1);
            try {
                m.setAccessible(true);
                n.set(name, toJson(m.invoke(o)));
            } catch (Exception ignored) {
                // 读不到就跳过
            }
        }
        return n;
    }

    /** 通用实体状态：反射读所有可读属性。 */
    static Map<String, Object> entityState(Object o) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (o == null) {
            return out;
        }
        for (Method m : o.getClass().getMethods()) {
            if (m.getParameterCount() != 0 || m.getDeclaringClass() == Object.class) {
                continue;
            }
            String name = m.getName();
            if (name.startsWith("get") && name.length() > 3) {
                name = name.substring(3);
            } else if (name.startsWith("is") && name.length() > 2) {
                name = name.substring(2);
            } else {
                continue;
            }
            name = Character.toLowerCase(name.charAt(0)) + name.substring(1);
            try {
                m.setAccessible(true);
                out.put(name, m.invoke(o));
            } catch (Exception ignored) {
                // 读不到就跳过
            }
        }
        return out;
    }

    static int sizeOf(Object v) {
        if (v == null) {
            return 0;
        }
        if (v instanceof Collection<?> c) {
            return c.size();
        }
        if (v instanceof Map<?, ?> m) {
            return m.size();
        }
        if (v instanceof String s) {
            return s.length();
        }
        if (v.getClass().isArray()) {
            return Array.getLength(v);
        }
        return 0;
    }

    static boolean truthy(Object v) {
        if (v == null) {
            return false;
        }
        if (v instanceof Boolean b) {
            return b;
        }
        if (v instanceof Collection<?> c) {
            return !c.isEmpty();
        }
        if (v instanceof Map<?, ?> m) {
            return !m.isEmpty();
        }
        return true;
    }

    static Field findField(Class<?> k, String name) {
        for (Class<?> c = k; c != null; c = c.getSuperclass()) {
            try {
                return c.getDeclaredField(name);
            } catch (NoSuchFieldException ignored) {
                // 往父类找
            }
        }
        return null;
    }

    static Method findSetter(Class<?> k, String prop) {
        String name = "set" + Character.toUpperCase(prop.charAt(0)) + prop.substring(1);
        for (Class<?> c = k; c != null; c = c.getSuperclass()) {
            for (Method m : c.getMethods()) {
                if (m.getName().equals(name) && m.getParameterCount() == 1) {
                    return m;
                }
            }
            for (Method m : c.getDeclaredMethods()) {
                if (m.getName().equals(name) && m.getParameterCount() == 1) {
                    return m;
                }
            }
        }
        return null;
    }

    static Method findDeclared(Class<?> type, String name) {
        for (Class<?> c = type; c != null; c = c.getSuperclass()) {
            for (Method m : c.getDeclaredMethods()) {
                if (m.getName().equals(name) && m.getParameterCount() == 0) {
                    return m;
                }
            }
        }
        return null;
    }

    static List<Method> allMethods(Class<?> type, String name) {
        List<Method> out = new ArrayList<>();
        for (Class<?> c = type; c != null; c = c.getSuperclass()) {
            for (Method m : c.getDeclaredMethods()) {
                if (m.getName().equals(name)) {
                    out.add(m);
                }
            }
        }
        return out;
    }

    /** 选方法：参数个数匹配的优先；尾部参数允许由框架类型自动补齐；public 优先。 */
    static Method pickMethod(List<Method> cands, int provided) {
        Method fallback = null;
        Method matched = null;
        for (Method m : cands) {
            if (fallback == null) {
                fallback = m;
            }
            Class<?>[] pt = m.getParameterTypes();
            if (pt.length < provided) {
                continue;
            }
            boolean ok = true;
            for (int i = provided; i < pt.length; i++) {
                if (!isAutoFillable(pt[i])) {
                    ok = false;
                    break;
                }
            }
            if (!ok) {
                continue;
            }
            if (Modifier.isPublic(m.getModifiers())) {
                return m;
            }
            if (matched == null) {
                matched = m;
            }
        }
        return matched != null ? matched : fallback;
    }

    /** 找任意可见性的方法（按名字 + 参数个数）。 */
    static Method findMethodAny(Class<?> type, String name, int argc) {
        for (Class<?> c = type; c != null; c = c.getSuperclass()) {
            for (Method m : c.getDeclaredMethods()) {
                if (m.getName().equals(name) && m.getParameterCount() == argc) {
                    return m;
                }
            }
        }
        return null;
    }

    static boolean isAutoFillable(Class<?> t) {
        String n = t.getName();
        return n.equals("jakarta.servlet.http.HttpServletRequest")
                || n.equals("jakarta.servlet.ServletRequest")
                || n.equals("jakarta.servlet.http.HttpServletResponse")
                || n.equals("jakarta.servlet.ServletResponse")
                || n.equals("jakarta.servlet.FilterChain")
                || n.equals("org.springframework.http.server.ServerHttpRequest")
                || n.equals("org.springframework.http.server.ServerHttpResponse");
    }

    /** 路径读取：a.b.c -> getA().getB().getC()；也支持直接的方法名（value 之类）。 */
    static Object readPath(Object o, String path) {
        if (o == null || path == null || path.isEmpty()) {
            return o;
        }
        Object cur = o;
        for (String seg : path.split("\\.")) {
            if (cur == null) {
                return null;
            }
            cur = readSegment(cur, seg);
        }
        return cur;
    }

    static Object readSegment(Object o, String seg) {
        if (o instanceof Map<?, ?> map) {
            return map.get(seg);
        }
        if (o instanceof List<?> list) {
            try {
                return list.get(Integer.parseInt(seg));
            } catch (RuntimeException ignored) {
                return null;
            }
        }
        String[] candidates = {seg, "get" + Character.toUpperCase(seg.charAt(0)) + seg.substring(1),
                "is" + Character.toUpperCase(seg.charAt(0)) + seg.substring(1)};
        for (String candidate : candidates) {
            try {
                Method m = o.getClass().getMethod(candidate);
                m.setAccessible(true);
                return m.invoke(o);
            } catch (NoSuchMethodException ignored) {
                // 试下一个
            } catch (Exception e) {
                return null;
            }
        }
        return null;
    }

    static Object callNoArg(Object target, String method) {
        if (target == null) {
            return null;
        }
        try {
            Method m = target.getClass().getMethod(method);
            m.setAccessible(true);
            return m.invoke(target);
        } catch (Exception e) {
            return null;
        }
    }

    static void recordError(ObjectNode res, Throwable t) {
        boolean business = false;
        try {
            Method getCode = t.getClass().getMethod("getErrorCode");
            Object code = getCode.invoke(t);
            Method getStatus = t.getClass().getMethod("getStatus");
            Object st = getStatus.invoke(t);
            if (st != null) {
                Object v = st.getClass().getMethod("value").invoke(st);
                res.put("http_status", ((Number) v).intValue());
            }
            res.put("error_type", t.getClass().getSimpleName());
            res.put("error_code", code instanceof Enum<?> e ? e.name() : String.valueOf(code));
            res.put("error_message", String.valueOf(t.getMessage()));
            business = true;
        } catch (Exception ignored) {
            // 不是业务异常
        }
        if (!business) {
            res.put("error_type", t.getClass().getName());
            res.put("error_message", String.valueOf(t.getMessage()));
            res.put("is_business_exception", false);
        }
    }

    /** JSON -> 普通 Java 值（数字保持数值类型，字符串不带引号）。 */
    static Object plain(JsonNode n) {
        if (n == null || n.isNull() || n.isMissingNode()) {
            return null;
        }
        if (n.isObject()) {
            Map<String, Object> out = new LinkedHashMap<>();
            n.fields().forEachRemaining(e -> out.put(e.getKey(), plain(e.getValue())));
            return out;
        }
        if (n.isArray()) {
            List<Object> out = new ArrayList<>();
            n.forEach(e -> out.add(plain(e)));
            return out;
        }
        if (n.isNumber()) {
            return n.numberValue();
        }
        if (n.isBoolean()) {
            return n.booleanValue();
        }
        return n.asText();
    }

    /** CORS 配置表：给容器对象也能取到（protected 的 getCorsConfigurations 也行）。 */
    static Map<?, ?> corsMap(Object src) {
        if (src instanceof Map<?, ?> m) {
            return m;
        }
        if (src == null) {
            return Map.of();
        }
        Method m = findMethodAny(src.getClass(), "getCorsConfigurations", 0);
        if (m != null) {
            try {
                m.setAccessible(true);
                Object v = m.invoke(src);
                if (v instanceof Map<?, ?> mm) {
                    return mm;
                }
            } catch (Exception ignored) {
                // 读不到就当没有
            }
        }
        return Map.of();
    }

    /** MethodParameter：只给类型（返回类型那一格）。 */
    static Object methodParameterOf(Class<?> type) throws Exception {
        Class<?> mp = Class.forName("org.springframework.core.MethodParameter");
        Method placeholder = ParameterPlaceholder.class.getDeclaredMethod("value", Class.class);
        Object p = mp.getConstructor(Method.class, int.class).newInstance(placeholder, -1);
        Method setType = mp.getDeclaredMethod("setParameterType", Class.class);
        setType.setAccessible(true);
        setType.invoke(p, type);
        return p;
    }

    /** 借一个真实 Method 当构造素材（MethodParameter 认 Method + 索引）。 */
    static class ParameterPlaceholder {
        static Class<?> value(Class<?> t) {
            return t;
        }
    }

    static void attachLogCapture(String pkg) {
        try {
            Class<?> ctxClass = Class.forName("ch.qos.logback.classic.LoggerContext");
            Object ctx = org.slf4j.LoggerFactory.getILoggerFactory();
            if (!ctxClass.isInstance(ctx)) {
                return;
            }
            Class<?> appenderClass = Class.forName("ch.qos.logback.core.read.ListAppender");
            Object appender = appenderClass.getDeclaredConstructor().newInstance();
            invokeContextAware(appender, ctx);
            appenderClass.getMethod("start").invoke(appender);
            Method getLogger = ctxClass.getMethod("getLogger", String.class);
            Object logger = getLogger.invoke(ctx, pkg);
            logger.getClass().getMethod("addAppender",
                    Class.forName("ch.qos.logback.core.Appender")).invoke(logger, appender);
            logAppender = appender;
        } catch (Throwable t) {
            logAppender = null;
        }
    }

    /** setContext(Context) / setContext(LoggerContext) —— 按名字 + 参数兼容度找。 */
    static void invokeContextAware(Object appender, Object ctx) throws Exception {
        for (Method m : appender.getClass().getMethods()) {
            if (!m.getName().equals("setContext") || m.getParameterCount() != 1) {
                continue;
            }
            if (!m.getParameterTypes()[0].isInstance(ctx)) {
                continue;
            }
            m.invoke(appender, ctx);
            return;
        }
        throw new NoSuchMethodException("setContext");
    }

    static List<String> capturedLogs() {
        List<String> out = new ArrayList<>();
        if (logAppender == null) {
            return out;
        }
        for (Object e : appenderEvents(logAppender)) {
            try {
                Object msg = e.getClass().getMethod("getFormattedMessage").invoke(e);
                out.add(String.valueOf(msg));
            } catch (Exception ignored) {
                // 读不到这一条就跳过
            }
        }
        return out;
    }

    static void clearLogs() {
        if (logAppender == null) {
            return;
        }
        try {
            @SuppressWarnings("unchecked")
            List<Object> events = (List<Object>) appenderEvents(logAppender);
            events.clear();
        } catch (Exception ignored) {
            // 清不掉就清不掉
        }
    }

    /** 抓到的日志事件：logback 1.5 起 list 是 public 字段，老版本才有 getList()。 */
    static List<?> appenderEvents(Object appender) {
        if (appender == null) {
            return List.of();
        }
        try {
            Method m = appender.getClass().getMethod("getList");
            Object v = m.invoke(appender);
            if (v instanceof List<?> l) {
                return l;
            }
        } catch (Exception ignored) {
            // 老版本路径不存在，走字段
        }
        try {
            Field f = appender.getClass().getField("list");
            Object v = f.get(appender);
            if (v instanceof List<?> l) {
                return l;
            }
        } catch (Exception ignored) {
            // 两条路都没有就当没日志
        }
        return List.of();
    }

    static void invokeIfPresent(Object target, String method, Class<?>[] types, Object... args) {
        try {
            Method m = target.getClass().getMethod(method, types);
            m.invoke(target, args);
        } catch (Exception ignored) {
            // 没有这个方法就算了
        }
    }

    static Object mockRequest(JsonNode spec) throws Exception {
        Class<?> k = Class.forName("org.springframework.mock.web.MockHttpServletRequest");
        Object req = k.getDeclaredConstructor(String.class, String.class)
                .newInstance(spec.path("method").asText("GET"), spec.path("uri").asText("/"));
        String remote = spec.path("remote_addr").asText("");
        if (!remote.isEmpty()) {
            invokeIfPresent(req, "setRemoteAddr", new Class<?>[]{String.class}, remote);
        }
        var headers = spec.path("headers").fields();
        while (headers.hasNext()) {
            var e = headers.next();
            addHeader(req, e.getKey(), e.getValue().asText());
        }
        return req;
    }

    /** addHeader 的签名随 Spring 版本变，按名字 + 参数个数找，值按参数类型转。 */
    static void addHeader(Object req, String name, String value) {
        for (Method m : req.getClass().getMethods()) {
            if (!m.getName().equals("addHeader") || m.getParameterCount() != 2) {
                continue;
            }
            Class<?>[] pt = m.getParameterTypes();
            if (pt[0] != String.class) {
                continue;
            }
            try {
                m.invoke(req, name, value);
                return;
            } catch (Exception ignored) {
                // 这个签名不接就试下一个
            }
        }
    }

    static Object mockResponse() throws Exception {
        Class<?> k = Class.forName("org.springframework.mock.web.MockHttpServletResponse");
        Object resp = k.getDeclaredConstructor().newInstance();
        LAST_RESPONSE = resp;
        return resp;
    }

    /** 框架类型参数自动补齐（Servlet 请求/响应、过滤器链、Spring 的请求响应包装）。 */
    static Object autoArg(Class<?> type, JsonNode step) {
        String name = type.getName();
        try {
            switch (name) {
                case "jakarta.servlet.http.HttpServletRequest":
                case "jakarta.servlet.ServletRequest": {
                    return mockRequest(step.path("request"));
                }
                case "jakarta.servlet.http.HttpServletResponse":
                case "jakarta.servlet.ServletResponse": {
                    Object resp = mockResponse();
                    LAST_RESPONSE = resp;
                    return resp;
                }
                case "jakarta.servlet.FilterChain": {
                    return Class.forName("org.springframework.mock.web.MockFilterChain")
                            .getDeclaredConstructor().newInstance();
                }
                case "org.springframework.http.server.ServerHttpRequest": {
                    Class<?> servletRequest = Class.forName("jakarta.servlet.http.HttpServletRequest");
                    Class<?> c = Class.forName("org.springframework.http.server.ServletServerHttpRequest");
                    Constructor<?> ctor = c.getDeclaredConstructor(servletRequest);
                    ctor.setAccessible(true);
                    return ctor.newInstance(mockRequest(step.path("request")));
                }
                case "org.springframework.http.server.ServerHttpResponse": {
                    Class<?> servletResponse = Class.forName("jakarta.servlet.http.HttpServletResponse");
                    Class<?> c = Class.forName("org.springframework.http.server.ServletServerHttpResponse");
                    Constructor<?> ctor = c.getDeclaredConstructor(servletResponse);
                    ctor.setAccessible(true);
                    Object resp = mockResponse();
                    LAST_RESPONSE = resp;
                    return ctor.newInstance(resp);
                }
                default:
                    return defaultValue(type);
            }
        } catch (Exception e) {
            return defaultValue(type);
        }
    }

    /** 把返回值上的路径抽成原始值。 */
    static JsonNode captureValue(JsonNode spec, Object value) {
        String path = spec.path("path").asText("");
        Object v = path.isEmpty() ? value : readPath(value, path);
        String mode = spec.path("op").asText("value");
        switch (mode) {
            case "count" -> {
                return M.getNodeFactory().numberNode(sizeOf(v));
            }
            case "json" -> {
                try {
                    return M.getNodeFactory().textNode(M.writeValueAsString(v));
                } catch (Exception e) {
                    return M.getNodeFactory().textNode(String.valueOf(v));
                }
            }
            case "text" -> {
                return M.getNodeFactory().textNode(String.valueOf(v));
            }
            case "is_null" -> {
                return M.getNodeFactory().booleanNode(v == null);
            }
            case "present" -> {
                return M.getNodeFactory().booleanNode(v != null);
            }
            case "class_name_contains" -> {
                String text = spec.path("text").asText("");
                String simple = v == null ? "" : v.getClass().getSimpleName();
                return M.getNodeFactory().booleanNode(simple.contains(text));
            }
            case "instance_of" -> {
                try {
                    Class<?> want = Class.forName(spec.path("class").asText());
                    return M.getNodeFactory().booleanNode(want.isInstance(v));
                } catch (ClassNotFoundException e) {
                    return M.getNodeFactory().booleanNode(false);
                }
            }
            default -> {
                return toJson(v);
            }
        }
    }

}
