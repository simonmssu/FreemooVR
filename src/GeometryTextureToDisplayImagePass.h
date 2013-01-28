/* -*- Mode: C++; tab-width: 4; indent-tabs-mode: nil; c-basic-offset: 4 -*- */

#ifndef GEOMETRY_TEXTURE_TO_DISPLAY_IMAGE_PASS_H
#define GEOMETRY_TEXTURE_TO_DISPLAY_IMAGE_PASS_H
#include <osg/Group>
#include <osg/Camera>
#include <osg/ref_ptr>
#include <osg/TextureRectangle>
#include <osg/Texture2D>

class GeometryTextureToDisplayImagePass {
public:
	GeometryTextureToDisplayImagePass(std::string shader_dir,
									  osg::ref_ptr<osg::Texture2D> input_texture,
									  std::string p2g_filename,
									  bool show_geom_coords=false);
	osg::ref_ptr<osg::Group> get_top() { return _top; }
	osg::ref_ptr<osg::TextureRectangle> get_output_texture() { return _out_texture; }
	int get_display_width() {return _display_width; }
	int get_display_height() {return _display_height; }
private:
	void create_output_texture();
	void setup_camera();
	void set_shader(std::string vert_filename, std::string frag_filename);
	osg::ref_ptr<osg::Group> create_input_geometry();

	osg::ref_ptr<osg::Group> _top;
	osg::Camera* _camera;
	osg::ref_ptr<osg::Texture2D> _input_texture;
	osg::ref_ptr<osg::TextureRectangle> _p2g_texture;
	osg::ref_ptr<osg::TextureRectangle> _out_texture;
	osg::ref_ptr<osg::Program> _program;
    osg::ref_ptr<osg::StateSet> _state_set;
	int _display_width;
	int _display_height;
	bool _show_geom_coords;
};

#endif