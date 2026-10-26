package com.trendpower.aiops.node;

import java.io.ByteArrayOutputStream;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.Flow;

/** 在读取中限制正文大小，不先把任意大的上游响应装进内存。 */
final class BoundedJsonBodyHandler implements HttpResponse.BodyHandler<byte[]> {
    private final int limit;
    BoundedJsonBodyHandler(int limit) { this.limit = limit; }

    @Override public HttpResponse.BodySubscriber<byte[]> apply(HttpResponse.ResponseInfo response) {
        return new HttpResponse.BodySubscriber<>() {
            private final CompletableFuture<byte[]> result = new CompletableFuture<>();
            private final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            private Flow.Subscription subscription;
            @Override public CompletionStage<byte[]> getBody() { return result; }
            @Override public void onSubscribe(Flow.Subscription source) {
                subscription = source;
                source.request(1);
            }
            @Override public void onNext(List<ByteBuffer> chunks) {
                for (ByteBuffer chunk : chunks) {
                    if (chunk.remaining() > limit - bytes.size()) {
                        subscription.cancel();
                        result.completeExceptionally(CpmsReadTool.refused("cpms_body_too_large"));
                        return;
                    }
                    byte[] value = new byte[chunk.remaining()];
                    chunk.get(value);
                    bytes.writeBytes(value);
                }
                subscription.request(1);
            }
            @Override public void onError(Throwable failure) { result.completeExceptionally(failure); }
            @Override public void onComplete() { result.complete(bytes.toByteArray()); }
        };
    }
}
