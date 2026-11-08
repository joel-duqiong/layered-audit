import java.io.ByteArrayInputStream;
import java.io.DataInputStream;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 读 .class 文件的常量池，取出全部 UTF8 常量（字符串字面量、类名、字段名……）。
 *
 * 为什么不用「把 class 文件当文本正则扫」：字节码里字符串常量后面紧跟的是控制字符，
 * 正则会把二进制尾巴一起吃进来（实测拿到过 "http://host:8098}\u0001\u0000\u0004rate"）。
 * 证据里出现这种东西，等于没有证据。
 */
final class ClassConstPool {

    private ClassConstPool() {
    }

    /** 解析失败就返回空列表 —— 拿不到常量不是结论。 */
    static List<String> utf8Constants(byte[] bytes) {
        List<String> out = new ArrayList<>();
        try (DataInputStream in = new DataInputStream(new ByteArrayInputStream(bytes))) {
            if (in.readInt() != 0xCAFEBABE) {
                return out;
            }
            in.readUnsignedShort();               // minor version
            in.readUnsignedShort();               // major version
            int count = in.readUnsignedShort();   // constant_pool_count
            String[] utf8 = new String[count];
            int[] tags = new int[count];
            for (int i = 1; i < count; i++) {
                int tag = in.readUnsignedByte();
                tags[i] = tag;
                switch (tag) {
                    case 1 -> utf8[i] = in.readUTF();               // Utf8
                    case 3, 4 -> in.skipBytes(4);                    // Integer / Float
                    case 5, 6 -> { in.skipBytes(8); i++; }           // Long / Double（占两槽）
                    case 7, 8, 16, 19, 20 -> in.skipBytes(2);        // Class/String/MethodType/Module/Package
                    case 9, 10, 11, 12, 17, 18 -> in.skipBytes(4);   // 各种 ref / Dynamic / InvokeDynamic
                    case 15 -> in.skipBytes(3);                      // MethodHandle
                    default -> {
                        return out;                                  // 未知 tag：不硬猜
                    }
                }
            }
            for (int i = 1; i < count; i++) {
                if (tags[i] == 1 && utf8[i] != null) {
                    out.add(utf8[i]);
                }
            }
        } catch (IOException | RuntimeException ignored) {
            // 不是合法 class 文件：放弃，不下结论
        }
        return out;
    }

    /**
     * UTF8 常量里挑出 URL。
     *
     * URL 常常不是整条常量 —— 比如 Spring 的 "${某配置项:http://host:port}"，
     * 所以要在常量内部截一段出来。字符集刻意保守：不含空白、不含 '}' 之类的结构符，
     * 免得把配置表达式的尾巴也当成 URL。
     */
    static List<String> urls(byte[] bytes) {
        List<String> out = new ArrayList<>();
        for (String s : utf8Constants(bytes)) {
            Matcher m = URL_IN_CONST.matcher(s);
            while (m.find()) {
                out.add(m.group());
            }
        }
        return out;
    }

    private static final Pattern URL_IN_CONST =
            Pattern.compile("https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]{2,}");
}
