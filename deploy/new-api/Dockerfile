# 运行时镜像：使用宿主机交叉编译好的静态二进制（scripts/build.sh），
# 避免在镜像内拉取 bun/golang 构建镜像。
FROM alpine:3.20
RUN apk add --no-cache tzdata ca-certificates
COPY new-api /new-api
RUN chmod +x /new-api
EXPOSE 3000
WORKDIR /data
ENTRYPOINT ["/new-api"]
