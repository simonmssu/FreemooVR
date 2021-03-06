/* -*- Mode: C -*- */
#version 140
uniform sampler2D liveCamera;
uniform sampler2D p2c;
in vec2 ProjectorCoord;
out vec4 MyFragColor;

void main(void)
{
  vec2 CamCoord = texture(p2c, ProjectorCoord).xy;
  MyFragColor = texture(liveCamera, CamCoord);
}
