import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.lang.annotation.Annotation;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.lang.reflect.ParameterizedType;
import java.lang.reflect.Type;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * 类层形状探针：用反射把「类的形状」导出成 JSON。
 *
 * 形状 = 继承层次 / 覆写关系 / 值相等 / 封装泄漏 / 缓存里的可变对象 /
 * 里氏替换的后置条件比对（同一个方法，覆写方与继承方的可见状态差多少）。
 *
 * 零项目字面量：类清单从 stdin 读，判定规则是语言级的（不是按项目配置）。
 * 方法调用只发生在「实体层次里声明的 public 方法」上 —— 其他方法可能有副作用，
 * 探针不该在审计的时候顺手把业务数据改了。
 */
public class ClassShape {

    static final ObjectMapper M = new ObjectMapper();
    static final String NO_ARG = "__no_arg__";

    public static void main(String[] args) throws Exception {
        List<String> names = readStdin();
        List<Class<?>> classes = new ArrayList<>();
        for (String n : names) {
            try {
                classes.add(Class.forName(n));
            } catch (Throwable ignored) {
                // 类不在 classpath 上：跳过，不让探针崩
            }
        }

        ObjectNode out = M.createObjectNode();
        ArrayNode arr = M.createArrayNode();
        for (Class<?> c : classes) arr.add(describe(c));
        out.set("classes", arr);

        ArrayNode lsp = M.createArrayNode();
        for (ObjectNode n : findLspViolations(classes)) lsp.add(n);
        out.set("lsp_violations", lsp);

        System.out.println(M.writeValueAsString(out));
    }

    static List<String> readStdin() throws Exception {
        List<String> names = new ArrayList<>();
        java.io.BufferedReader in = new java.io.BufferedReader(
                new java.io.InputStreamReader(System.in));
        String line;
        while ((line = in.readLine()) != null) {
            line = line.trim();
            if (!line.isEmpty()) names.add(line);
        }
        return names;
    }

    // ------------------------------------------------------------- 单类形状
    static ObjectNode describe(Class<?> c) throws Exception {
        ObjectNode o = M.createObjectNode();
        o.put("class", c.getName());
        o.put("simple_name", c.getSimpleName());
        o.put("super", c.getSuperclass() == null ? null : c.getSuperclass().getName());
        List<String> ifs = new ArrayList<>();
        for (Class<?> i : c.getInterfaces()) ifs.add(i.getName());
        o.set("interfaces", M.valueToTree(ifs));
        o.set("annotations", M.valueToTree(annotationNames(c.getAnnotations())));
        o.put("is_entity", hasAnnotation(c, "Entity"));
        o.put("is_mapped_superclass", hasAnnotation(c, "MappedSuperclass"));
        o.put("declares_equals", declaresEqualsOrHashCode(c, "equals"));
        o.put("declares_hashcode", declaresEqualsOrHashCode(c, "hashCode"));

        ArrayNode fields = M.createArrayNode();
        for (Field f : c.getDeclaredFields()) {
            if (f.isSynthetic()) continue;
            ObjectNode fo = M.createObjectNode();
            fo.put("name", f.getName());
            fo.put("type", f.getType().getName());
            fields.add(fo);
        }
        o.set("fields", fields);

        ArrayNode methods = M.createArrayNode();
        ArrayNode leaks = M.createArrayNode();
        ArrayNode caches = M.createArrayNode();
        for (Method m : c.getDeclaredMethods()) {
            if (m.isSynthetic() || m.isBridge()) continue;
            if (m.getName().startsWith("lambda$")) continue;
            ObjectNode mo = M.createObjectNode();
            mo.put("name", m.getName());
            mo.put("returns", typeName(m.getGenericReturnType()));
            List<String> ps = new ArrayList<>();
            for (Class<?> p : m.getParameterTypes()) ps.add(p.getName());
            mo.set("params", M.valueToTree(ps));
            mo.put("public", Modifier.isPublic(m.getModifiers()));
            mo.put("static", Modifier.isStatic(m.getModifiers()));
            mo.set("annotations", M.valueToTree(annotationNames(m.getAnnotations())));
            methods.add(mo);

            if (!Modifier.isPublic(m.getModifiers()) || Modifier.isStatic(m.getModifiers())) continue;

            // 封装泄漏：public 方法把内部可变实体的「集合」交出去
            // （返回单个实体的 getter 是 JPA 常规，不算泄漏 —— 否则全是误报）
            String leaked = entityCollectionLeak(m.getGenericReturnType());
            if (leaked != null) {
                ObjectNode lo = M.createObjectNode();
                lo.put("class", c.getName());
                lo.put("method", m.getName());
                lo.put("returns", typeName(m.getGenericReturnType()));
                lo.put("entity_type", leaked);
                leaks.add(lo);
            }

            // 缓存里放可变对象：@Cacheable 的返回值可以被调用方改
            if (hasAnnotation(m, "Cacheable") || hasAnnotation(m, "CachePut")) {
                ObjectNode co = M.createObjectNode();
                co.put("class", c.getName());
                co.put("method", m.getName());
                co.put("returns", typeName(m.getGenericReturnType()));
                co.put("mutable", isMutableContainer(m.getGenericReturnType())
                        || entityCollectionLeak(m.getGenericReturnType()) != null);
                co.put("element_type", containerElementName(m.getGenericReturnType()));
                co.put("element_is_record", isRecord(containerElementClass(m.getGenericReturnType())));
                caches.add(co);
            }
        }
        o.set("methods", methods);
        o.set("encapsulation_leaks", leaks);
        o.set("cacheable_methods", caches);
        return o;
    }

    // ------------------------------------------------------- 里氏替换比对
    static List<ObjectNode> findLspViolations(List<Class<?>> classes) {
        List<ObjectNode> out = new ArrayList<>();

        // 签名 -> 声明它的类（按继承深度从浅到深）
        Map<String, Map<Class<?>, Method>> sigs = new LinkedHashMap<>();
        for (Class<?> c : classes) {
            for (Method m : c.getDeclaredMethods()) {
                if (m.isSynthetic() || m.isBridge()) continue;
                if (!Modifier.isPublic(m.getModifiers())) continue;
                if (Modifier.isStatic(m.getModifiers())) continue;
                if (m.getParameterCount() != 1) continue;
                // 只在实体层次里比对：其他方法的调用可能有副作用
                if (!isPersistentish(m.getDeclaringClass())) continue;
                String sig = signature(m);
                sigs.computeIfAbsent(sig, k -> new LinkedHashMap<>()).put(c, m);
            }
        }

        for (Map.Entry<String, Map<Class<?>, Method>> e : sigs.entrySet()) {
            Map<Class<?>, Method> declarers = e.getValue();
            if (declarers.size() < 2) continue;      // 没有覆写关系可比
            for (Map.Entry<Class<?>, Method> d : declarers.entrySet()) {
                Class<?> over = d.getKey();
                Method overM = d.getValue();
                // 祖先声明者（父类的承诺）
                Class<?> base = null;
                Method baseM = null;
                for (Map.Entry<Class<?>, Method> a : declarers.entrySet()) {
                    if (a.getKey() == over) continue;
                    if (isAncestor(a.getKey(), over) && (base == null || isAncestor(base, a.getKey()))) {
                        base = a.getKey();
                        baseM = a.getValue();
                    }
                }
                if (base == null) continue;
                // 找一个「继承而不覆写」的同类：它执行的是父类的承诺
                for (Class<?> inh : classes) {
                    if (inh == over || inh == base) continue;
                    if (declarers.containsKey(inh)) continue;
                    if (!isAncestor(base, inh)) continue;
                    Object a = newInstance(over);
                    Object b = newInstance(inh);
                    if (a == null || b == null) continue;
                    Object arg = synthArg(overM.getParameterTypes()[0]);
                    if (arg == null || NO_ARG.equals(arg)) continue;
                    Map<String, String> sa = invokeAndSnap(a, overM, arg);
                    Map<String, String> sb = invokeAndSnap(b, baseM, arg);
                    if (sa == null || sb == null) continue;
                    // 只比两边共有的字段：各自独有的字段不是「继承来的承诺」，
                    // 把它们算进差异会把「类不同」误报成「覆写改了语义」
                    List<String> diff = new ArrayList<>();
                    for (String k : sa.keySet()) {
                        if (!sb.containsKey(k)) continue;
                        if (!Objects_equal(sa.get(k), sb.get(k))) diff.add(k);
                    }
                    if (diff.isEmpty()) continue;
                    ObjectNode o = M.createObjectNode();
                    o.put("overrider", over.getName());
                    o.put("base", base.getName());
                    o.put("inheritor", inh.getName());
                    o.put("method", overM.getName());
                    o.put("params", typeName(overM.getGenericParameterTypes()[0]));
                    o.put("arg", String.valueOf(arg));
                    o.set("differing_fields", M.valueToTree(diff));
                    // 逐字段给出两边的实际取值 —— 报告里要能说清「哪里不一样」
                    ObjectNode states = M.createObjectNode();
                    for (String k : diff) {
                        ObjectNode one = M.createObjectNode();
                        one.put("overrider", sa.get(k));
                        one.put("inheritor", sb.get(k));
                        states.set(k, one);
                    }
                    o.set("field_states", states);
                    o.put("overrider_state", String.valueOf(sa));
                    o.put("inheritor_state", String.valueOf(sb));
                    o.put("inherited_state", String.valueOf(sb));
                    out.add(o);
                }
            }
        }
        return out;
    }

    static boolean Objects_equal(String a, String b) {
        return a == null ? b == null : a.equals(b);
    }

    static String signature(Method m) {
        StringBuilder sb = new StringBuilder(m.getName()).append("(");
        for (Class<?> p : m.getParameterTypes()) sb.append(p.getName()).append(",");
        return sb.append(")").toString();
    }

    static boolean declaresEqualsOrHashCode(Class<?> c, String name) {
        for (Method m : c.getDeclaredMethods()) {
            if (m.getName().equals(name) && m.getParameterCount() == 0) return true;
        }
        return false;
    }

    static boolean isAncestor(Class<?> ancestor, Class<?> sub) {
        Class<?> cur = sub.getSuperclass();
        while (cur != null) {
            if (cur.equals(ancestor)) return true;
            cur = cur.getSuperclass();
        }
        return false;
    }

    static boolean isPersistentish(Class<?> c) {
        return hasAnnotation(c, "Entity") || hasAnnotation(c, "MappedSuperclass");
    }

    static Object newInstance(Class<?> c) {
        try {
            Constructor<?> ctor = c.getDeclaredConstructor();
            ctor.setAccessible(true);
            return ctor.newInstance();
        } catch (Throwable t) {
            return null;
        }
    }

    /** 造一个可比较的参数：只支持 String / 基本类型（避免调用有副作用的方法）。 */
    static Object synthArg(Class<?> t) {
        if (t == String.class) return "actor";
        if (t == int.class || t == Integer.class) return 1;
        if (t == long.class || t == Long.class) return 1L;
        if (t == boolean.class || t == Boolean.class) return Boolean.FALSE;
        if (t == double.class || t == Double.class) return 1.0d;
        return NO_ARG;
    }

    /** 调用方法，然后快照可见状态（实例字段，含继承来的）。 */
    static Map<String, String> invokeAndSnap(Object target, Method m, Object arg) {
        try {
            m.setAccessible(true);
            m.invoke(target, arg);
        } catch (Throwable t) {
            return null;
        }
        Map<String, String> snap = new LinkedHashMap<>();
        for (Class<?> cur = target.getClass(); cur != null && cur != Object.class;
                cur = cur.getSuperclass()) {
            for (Field f : cur.getDeclaredFields()) {
                if (f.isSynthetic() || Modifier.isStatic(f.getModifiers())) continue;
                try {
                    f.setAccessible(true);
                    Object v = f.get(target);
                    snap.put(f.getName(), safeValue(v, f.getType()));
                } catch (Throwable ignored) {
                    // 读不到就跳过
                }
            }
        }
        return snap;
    }

    static String safeValue(Object v, Class<?> declared) {
        if (v == null) return "null";
        Class<?> t = declared;
        while (t != null && !t.isPrimitive() && t != Object.class) {
            if (t == String.class || t == Number.class || t == Boolean.class
                    || t == Character.class || t.isEnum()) {
                return String.valueOf(v);
            }
            t = t.getSuperclass();
        }
        if (t != null && t.isPrimitive()) return String.valueOf(v);
        return "ref(non-null)";       // 引用型实体只比 null / 非 null
    }

    // ------------------------------------------------------------ 小工具
    static boolean hasAnnotation(Class<?> c, String simpleName) {
        for (String n : annotationNames(c.getAnnotations())) if (n.equals(simpleName)) return true;
        return false;
    }

    static boolean hasAnnotation(Method m, String simpleName) {
        for (String n : annotationNames(m.getAnnotations())) if (n.equals(simpleName)) return true;
        return false;
    }

    static List<String> annotationNames(Annotation[] anns) {
        List<String> out = new ArrayList<>();
        for (Annotation a : anns) out.add(a.annotationType().getSimpleName());
        return out;
    }

    static String typeName(Type t) {
        if (t instanceof ParameterizedType) {
            Type raw = ((ParameterizedType) t).getRawType();
            StringBuilder sb = new StringBuilder(raw instanceof Class
                    ? ((Class<?>) raw).getName() : String.valueOf(raw));
            sb.append("<");
            Type[] args = ((ParameterizedType) t).getActualTypeArguments();
            for (int i = 0; i < args.length; i++) {
                if (i > 0) sb.append(",");
                sb.append(typeName(args[i]));
            }
            return sb.append(">").toString();
        }
        if (t instanceof Class) return ((Class<?>) t).getName();
        return String.valueOf(t);
    }

    static String containerElementName(Type t) {
        if (t instanceof ParameterizedType) {
            Type[] args = ((ParameterizedType) t).getActualTypeArguments();
            if (args.length == 1) return typeName(args[0]);
        }
        return null;
    }

    static boolean isMutableContainer(Type t) {
        if (!(t instanceof ParameterizedType)) return false;
        Class<?> raw = (Class<?>) ((ParameterizedType) t).getRawType();
        return java.util.Collection.class.isAssignableFrom(raw)
                || java.util.Map.class.isAssignableFrom(raw);
    }

    /** 容器里装的是不是实体 —— 实体就是内部可变状态。 */
    static String entityLeak(Type t) {
        if (t instanceof Class) {
            Class<?> c = (Class<?>) t;
            if (c.isPrimitive() || c.getName().startsWith("java.")) return null;
            try {
                if (hasAnnotation(c, "Entity")) return c.getName();
            } catch (Throwable ignored) {
                return null;
            }
        }
        return null;
    }

    /**
     * public 方法把「实体的集合」交出去 —— 这才是封装泄漏。
     * 只返回单个实体引用（getter）不算：那是 JPA 的常规用法。
     */
    static String entityCollectionLeak(Type t) {
        if (t instanceof ParameterizedType) {
            Type raw = ((ParameterizedType) t).getRawType();
            boolean container = raw instanceof Class && (java.util.Collection.class.isAssignableFrom((Class<?>) raw)
                    || java.util.Map.class.isAssignableFrom((Class<?>) raw)
                    || Optional.class.isAssignableFrom((Class<?>) raw)
                    || Iterable.class.isAssignableFrom((Class<?>) raw));
            if (!container) return null;
            Type[] args = ((ParameterizedType) t).getActualTypeArguments();
            for (Type a : args) {
                String e = entityLeak(a);
                if (e != null) return e;
            }
            return null;
        }
        if (t instanceof Class && ((Class<?>) t).isArray()) {
            Class<?> comp = ((Class<?>) t).getComponentType();
            return entityLeak(comp);
        }
        return null;
    }

    static Class<?> containerElementClass(Type t) {
        if (t instanceof ParameterizedType) {
            Type[] args = ((ParameterizedType) t).getActualTypeArguments();
            if (args.length == 1 && args[0] instanceof Class) return (Class<?>) args[0];
        }
        return null;
    }

    static boolean isRecord(Class<?> c) {
        if (c == null) return false;
        try {
            return c.isRecord();
        } catch (Throwable t) {
            return false;
        }
    }
}
